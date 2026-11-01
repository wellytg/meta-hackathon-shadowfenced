"""T6 UI-review fixes: user-facing banner text, choice reasons, UI hooks.

Secrets are built at runtime from the canary registry; no secret literal
appears in this file.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from shadowfenced import engine
from shadowfenced.app import REDACT_NOTE, SendRequest, app, send
from shadowfenced.canary import CanaryRegistry
from shadowfenced.engine import describe_placeholders, gate, user_message
from shadowfenced.policy import load_policy

SEED = "test-seed-t6"
KINDS = ("db_password", "api_key")
ENTROPY_TOKEN = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"
ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"


@pytest.fixture()
def policy():
    return load_policy(ROOT / "policy.yaml")


@pytest.fixture()
def registry():
    return CanaryRegistry(SEED, KINDS)


@pytest.fixture()
def mock_upstream():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    app.state.upstream_transport = httpx.MockTransport(handler)
    yield
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")


def test_user_message_block(policy, registry):
    canary = registry.value("db_password")
    decision = gate(f"password: {canary}", policy, registry)
    assert decision.decision == "BLOCK"
    assert user_message(decision) == (
        "A planted canary token was found (proven by code). Nothing was sent."
    )


def test_user_message_redact_breakdown(policy, registry):
    decision = gate(
        "mail ada@corvanelabs.com about Project Tallow", policy, registry
    )
    assert decision.decision == "REDACT"
    assert user_message(decision) == (
        "Sent with 2 redactions: 1 email address, 1 internal codename."
    )


def test_user_message_redact_singular(policy, registry):
    decision = gate("mail ada@corvanelabs.com now", policy, registry)
    assert decision.decision == "REDACT"
    assert user_message(decision) == "Sent with 1 redaction: 1 email address."


def test_user_message_hold(policy, registry):
    decision = gate(f"here is {ENTROPY_TOKEN} maybe", policy, registry)
    assert decision.decision == "HOLD"
    assert user_message(decision) == (
        "Held for review: possible pointer to sensitive data."
    )


def test_user_message_allow(policy, registry):
    decision = gate("hello world, nothing sensitive", policy, registry)
    assert decision.decision == "ALLOW"
    assert user_message(decision) == "Sent clean"


def test_user_message_has_no_ids_or_values(policy, registry):
    decision = gate(
        "mail ada@corvanelabs.com about Project Tallow", policy, registry
    )
    message = user_message(decision)
    assert decision.decision_id not in message
    for span in decision.spans:
        assert span.span_id not in message
    assert "ada@corvanelabs.com" not in message
    assert "Tallow" not in message


def test_gate_without_choices_claims_no_user_choice(policy, registry):
    decision = gate("mail ada@corvanelabs.com now", policy, registry)
    assert decision.decision == "REDACT"
    assert not any("user redacted" in r for r in decision.reasons)
    assert not any("user kept" in r for r in decision.reasons)
    assert not any("you chose" in r for r in decision.reasons)


def test_gate_with_explicit_choice_records_it(policy, registry):
    from shadowfenced import tier1

    text = "mail ada@corvanelabs.com now"
    spans = tier1.scan(text, policy, registry)
    span_id = next(s.span_id for s in spans if s.subclass == "email")
    decision = gate(
        text, policy, registry, [{"span_id": span_id, "choice": "redact"}]
    )
    assert any("user redacted" in r for r in decision.reasons)


def test_describe_placeholders_plural():
    assert describe_placeholders(
        {"[PII_EMAIL_1]": "a@x.com", "[PII_EMAIL_2]": "b@x.com"}
    ) == ["2 email addresses"]
    assert describe_placeholders({"[CODENAME_1]": "X"}) == [
        "1 internal codename"
    ]


def test_send_response_carries_user_message(mock_upstream):
    body = send(
        SendRequest(
            messages=[
                {
                    "role": "user",
                    "content": "mail ada@corvanelabs.com about Project Tallow",
                }
            ]
        )
    )
    assert body["decision"] == "REDACT"
    assert body["user_message"] == (
        "Sent with 2 redactions: 1 email address, 1 internal codename."
    )
    # Machine reasons stay in the evidence payload (the log), not the banner.
    assert body["evidence"]["reasons"]


def test_redact_note_firm_wording():
    assert "Privacy notice from the user's organisation" in REDACT_NOTE
    assert "[PII_EMAIL_1]" in REDACT_NOTE
    assert "harmless placeholder labels" in REDACT_NOTE
    assert "not private data" in REDACT_NOTE
    assert "copy the labels exactly" in REDACT_NOTE


def test_composer_text_visible_and_backdrop_aligned():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    # Only the backdrop and its marks stay transparent (2 occurrences);
    # the textarea itself renders normal ink text above the backdrop.
    assert css.count("color: transparent") == 2
    assert "color: var(--ink)" in css
    assert "z-index: 1" in css
    for prop in (
        "font-size: 14px",
        "line-height: 1.5",
        "letter-spacing: normal",
        "padding: 8px 10px",
        "border-width: 0",
        "white-space: pre-wrap",
        "word-wrap: break-word",
    ):
        assert prop in css


def test_header_pill_and_empty_hint_present():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-testid="header-pill"' in html
    # T38 status indicator: model + guard state (from the model picker
    # and the local guard probe).
    assert "Model: Muse Spark · cloud" in html
    assert "Guard: on · checking locally (code + Muse Glimmer)" in html
    assert 'data-testid="guard-dot"' in html
    assert 'data-testid="empty-hint"' in html
    assert (
        "Type or paste anything. Sensitive text is underlined as you type, "
        "and only clean or redacted text is sent to the AI." in html
    )
    # Existing hooks are kept.
    for hook in (
        'data-testid="composer"',
        'data-testid="send"',
        'data-testid="provider-select"',
        'data-testid="stats-tier1"',
        'data-testid="stats-holds"',
        'data-testid="log-ok"',
    ):
        assert hook in html


def test_banner_never_renders_machine_reasons():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "evidence.reasons.join" not in js
    assert "user_message" in js
    assert "hasOwnProperty.call(choices" in js
    assert "clearComposer" in js
    assert "updateEmptyHint" in js
    for hook in (
        'data-testid", "banner"',
        'data-testid", "ai-saw-toggle"',
        'data-testid", "ai-saw"',
        'data-testid", "answer"',
        'data-testid", "btn-redact"',
        'data-testid", "btn-keep"',
        'data-testid", "btn-why"',
        'data-testid", "why-text"',
    ):
        assert hook in js


def test_engine_exports_user_message():
    assert "user_message" in engine.__all__
    assert "describe_placeholders" in engine.__all__
