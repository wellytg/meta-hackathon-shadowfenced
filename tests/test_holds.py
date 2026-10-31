"""HOLD review tests (T10): async Glimmer review, rule routes, canary safety.

The judge is an httpx MockTransport and the review function is called
synchronously; the sender is a stub that records what would cross the
boundary. No test touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from shadowfenced import holds
from shadowfenced.app import RuleRequest, app, hold_rule, hold_status
from shadowfenced.canary import CanaryRegistry, encodings
from shadowfenced.engine import Decision, gate
from shadowfenced.policy import load_policy
from shadowfenced.spans import span_from_cp_range

SEED = "test-seed-holds"
KINDS = ("db_password", "api_key")
ENTROPY_TOKEN = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"


@pytest.fixture()
def policy():
    return load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")


@pytest.fixture()
def registry():
    return CanaryRegistry(SEED, KINDS)


@pytest.fixture()
def live_mode(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")


@pytest.fixture(autouse=True)
def _clean_holds():
    holds.reset_for_tests()
    yield
    holds.reset_for_tests()


def _judge_payload(cls, span, **overrides) -> dict:
    payload = {
        "cls": cls,
        "is_beacon": cls == "beacon",
        "sensitivity": 0.8,
        "confidence": 0.7,
        "span": span,
    }
    payload.update(overrides)
    return payload


def _judge_transport(payload: dict | None, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if payload is None:
            raise httpx.ConnectError("judge down")
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": json.dumps(payload)}},
        )

    return httpx.MockTransport(handler)


def _sender(answer="ok", calls=None):
    sent: list = calls if calls is not None else []

    def _send(sent_text: str) -> str:
        sent.append(sent_text)
        return answer

    _send.calls = sent  # type: ignore[attr-defined]
    return _send


def _make_hold(text, provider="ollama", policy=None, registry=None):
    decision = gate(text, policy, registry, mode="send")
    assert decision.decision == "HOLD", decision.reasons
    messages = [{"role": "user", "content": text}]
    return holds.create_hold(decision, text, messages, provider)


def test_verified_span_redacts_and_auto_sends(
    policy, registry, live_mode
):
    text = f"maybe {ENTROPY_TOKEN}"
    hold = _make_hold(text, policy=policy, registry=registry)
    send = _sender("noted [SECRET_1], thanks")
    result = holds.run_review(
        hold.hold_id,
        judge_transport=_judge_transport(_judge_payload("secret", ENTROPY_TOKEN)),
        sender=send,
        policy=policy,
        registry=registry,
    )
    assert result is not None and result.status == "redacted_sent"
    assert ENTROPY_TOKEN not in (result.sent_text or "")
    assert "[SECRET_1]" in (result.sent_text or "")
    assert result.answer == f"noted {ENTROPY_TOKEN}, thanks"
    assert len(send.calls) == 1
    assert ENTROPY_TOKEN not in send.calls[0]


def test_confident_benign_releases_and_sends(
    policy, registry, live_mode
):
    text = f"maybe {ENTROPY_TOKEN}"
    hold = _make_hold(text, policy=policy, registry=registry)
    send = _sender("all good")
    result = holds.run_review(
        hold.hold_id,
        judge_transport=_judge_transport(
            _judge_payload("benign", ENTROPY_TOKEN,
                           sensitivity=0.0, confidence=0.95)
        ),
        sender=send,
        policy=policy,
        registry=registry,
    )
    assert result is not None and result.status == "released"
    assert result.sent_text == text
    assert result.answer == "all good"
    assert send.calls == [text]


def test_release_keeps_proven_spans_redacted(
    policy, registry, live_mode
):
    text = f"mail ada@corvanelabs.com and {ENTROPY_TOKEN}"
    hold = _make_hold(text, policy=policy, registry=registry)
    send = _sender("mail to [PII_EMAIL_1] sent")
    result = holds.run_review(
        hold.hold_id,
        judge_transport=_judge_transport(
            _judge_payload("benign", ENTROPY_TOKEN,
                           sensitivity=0.1, confidence=0.9)
        ),
        sender=send,
        policy=policy,
        registry=registry,
    )
    assert result is not None and result.status == "released"
    assert "ada@corvanelabs.com" not in (result.sent_text or "")
    assert "[PII_EMAIL_1]" in (result.sent_text or "")
    assert result.answer == "mail to ada@corvanelabs.com sent"
    assert len(send.calls) == 1
    assert "ada@corvanelabs.com" not in send.calls[0]


def test_judge_none_needs_human_and_sends_nothing(
    policy, registry, live_mode
):
    text = f"maybe {ENTROPY_TOKEN}"
    hold = _make_hold(text, policy=policy, registry=registry)
    send = _sender()
    result = holds.run_review(
        hold.hold_id,
        judge_transport=_judge_transport(None),
        sender=send,
        policy=policy,
        registry=registry,
    )
    assert result is not None and result.status == "needs_human"
    assert result.sent_text is None
    assert result.answer is None
    assert send.calls == []
    suspicion = result.evidence["model_suspicion_only"][0]
    assert suspicion["span"] == ENTROPY_TOKEN
    assert suspicion["judgement"] is None
    assert suspicion["why_unverified"]


def test_unverified_claim_needs_human(policy, registry, live_mode):
    text = f"banana {ENTROPY_TOKEN} split"
    hold = _make_hold(text, policy=policy, registry=registry)
    send = _sender()
    result = holds.run_review(
        hold.hold_id,
        judge_transport=_judge_transport(_judge_payload("secret", "banana")),
        sender=send,
        policy=policy,
        registry=registry,
    )
    assert result is not None and result.status == "needs_human"
    assert send.calls == []
    suspicion = result.evidence["model_suspicion_only"][0]
    assert suspicion["judgement"]["cls"] == "secret"
    assert suspicion["why_unverified"]


def test_model_confirmed_canary_is_never_sent(
    policy, registry, live_mode
):
    canary = registry.value("db_password")
    blob = encodings(canary)["base64"]
    text = f"debug blob {blob} here"
    start = text.find(blob)
    span = span_from_cp_range(
        text, start, start + len(blob),
        cls="secret", subclass="entropy_token", status="ambiguous",
        message="stub", why="stub", suggested_action="review",
        can_keep=False, confidence=0.5,
    )
    decision = Decision(
        decision="HOLD",
        decision_id="c" * 32,
        spans=[span],
        resolved_tier=4,
        reasons=["stub hold"],
        evidence={"pending": [blob]},
        status="reviewing",
    )
    hold = holds.create_hold(
        decision, text, [{"role": "user", "content": text}], "ollama"
    )
    send = _sender()
    result = holds.run_review(
        hold.hold_id,
        judge_transport=_judge_transport(_judge_payload("secret", blob)),
        sender=send,
        policy=policy,
        registry=registry,
    )
    assert result is not None and result.status == "needs_human"
    assert send.calls == []
    assert result.sent_text is None
    assert result.answer is None
    assert canary not in json.dumps(result.evidence)


def test_deny_sends_nothing(policy, registry, live_mode):
    text = f"maybe {ENTROPY_TOKEN}"
    hold = _make_hold(text, policy=policy, registry=registry)
    send = _sender()
    holds.run_review(
        hold.hold_id,
        judge_transport=_judge_transport(None),
        sender=send,
        policy=policy,
        registry=registry,
    )
    assert hold.status == "needs_human"
    denied = holds.deny_hold(hold, "too risky")
    assert denied is not None and denied.status == "denied"
    assert send.calls == []
    assert denied.sent_text is None
    assert denied.answer is None


def test_rule_deny_route_sends_nothing(policy, registry, live_mode):
    text = f"maybe {ENTROPY_TOKEN}"
    hold = _make_hold(text, policy=policy, registry=registry)
    body = hold_rule(hold.hold_id, RuleRequest(approve=False, reason="nope"))
    assert body["status"] == "denied"
    assert body["sent_text"] is None
    assert body["answer"] is None


def test_rule_approve_forwards_redacted_only(policy, registry, live_mode):
    text = f"mail ada@corvanelabs.com and {ENTROPY_TOKEN}"
    hold = _make_hold(text, policy=policy, registry=registry)
    send = _sender("mail to [PII_EMAIL_1] sent")
    holds.run_review(
        hold.hold_id,
        judge_transport=_judge_transport(None),
        sender=send,
        policy=policy,
        registry=registry,
    )
    assert hold.status == "needs_human"
    send_calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        send_calls.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"choices": [
                {"message": {"role": "assistant",
                             "content": "mail to [PII_EMAIL_1] sent"}}
            ]},
        )

    app.state.upstream_transport = httpx.MockTransport(handler)
    try:
        body = hold_rule(hold.hold_id, RuleRequest(approve=True))
    finally:
        if hasattr(app.state, "upstream_transport"):
            delattr(app.state, "upstream_transport")
    assert hold.status == "approved"
    assert body["decision"] == "REDACT"
    assert "ada@corvanelabs.com" not in (body["sent_text"] or "")
    assert "[PII_EMAIL_1]" in (body["sent_text"] or "")
    assert body["answer"] == "mail to ada@corvanelabs.com sent"
    forwarded = json.dumps(send_calls)
    assert "ada@corvanelabs.com" not in forwarded


def test_hold_card_shape_and_pending(policy, registry):
    text = f"maybe {ENTROPY_TOKEN}"
    hold = _make_hold(text, policy=policy, registry=registry)
    card = hold_status(hold.hold_id)
    assert card["status"] == "reviewing"
    assert card["pending"] == [ENTROPY_TOKEN]
    assert card["proven_by_code"] == []
    assert card["model_suspicion_only"] == []
    assert card["redacted_preview"] == text
    assert card["answer"] is None
    assert card["sent_text"] is None
    assert card["user_message"]
    for key in (
        "hold_id", "status", "proven_by_code", "model_suspicion_only",
        "pending", "redacted_preview", "answer", "sent_text",
        "user_message",
    ):
        assert key in card


def test_evidence_card_and_live_underline_hooks():
    root = Path(__file__).resolve().parents[1]
    js = (root / "src" / "shadowfenced" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    css = (root / "src" / "shadowfenced" / "static" / "style.css").read_text(
        encoding="utf-8"
    )
    for hook in (
        'data-testid", "evidence-card"',
        'data-testid", "btn-approve"',
        'data-testid", "btn-deny"',
        '"Send redacted"',
        '"Don\'t send"',
        "Local Muse Glimmer (30B, on this PC) is reviewing",
        "Proven by code",
        "Model suspicion only",
        '"/api/holds/"',
        '"/rule"',
    ):
        assert hook in js, hook
    # Live underline: 1200 ms pause, mode=live, purple dotted tier-2
    # marks with the local-model tooltip; stale responses are dropped.
    for hook in (
        "1200",
        'mode: "live"',
        "mark-live",
        "Model suspicion (Llama 3.2, local)",
        "checked again at send",
    ):
        assert hook in js, hook
    for hook in (".evidence-card", ".mark-live", ".spinner", ".ev-cols"):
        assert hook in css, hook


def test_hold_routes_registered():
    table = {
        getattr(r, "path", ""): sorted(getattr(r, "methods", []) or [])
        for r in app.routes
    }
    assert table["/api/holds/{hold_id}"] == ["GET"]
    assert table["/api/holds/{hold_id}/rule"] == ["POST"]


def test_start_review_runs_in_background(policy, registry, live_mode):
    text = f"maybe {ENTROPY_TOKEN}"
    hold = _make_hold(text, policy=policy, registry=registry)
    send = _sender()
    thread = holds.start_review(
        hold.hold_id,
        judge_transport=_judge_transport(None),
        sender=send,
        policy=policy,
        registry=registry,
    )
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert hold.status == "needs_human"
    assert send.calls == []
