"""HOLD polish tests: approve replaces suspicion spans, stats single count.

The judge is an httpx MockTransport and review runs synchronously; the
sender is a stub. No test touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from shadowfenced import holds
from shadowfenced.app import hold_status
from shadowfenced.canary import CanaryRegistry
from shadowfenced.engine import gate
from shadowfenced.policy import load_policy
from shadowfenced.stats import stats_from_log

SEED = "test-seed-polish"
KINDS = ("db_password", "api_key")
ENTROPY_TOKEN = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"
ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"


def _policy():
    return load_policy(ROOT / "policy.yaml")


def _registry():
    return CanaryRegistry(SEED, KINDS)


def _clean():
    holds.reset_for_tests()


def _payload(cls, span, **overrides):
    payload = {
        "cls": cls,
        "is_beacon": cls == "beacon",
        "sensitivity": 0.8,
        "confidence": 0.7,
        "span": span,
    }
    payload.update(overrides)
    return payload


def _transport(payload):
    def handler(request: httpx.Request) -> httpx.Response:
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


@pytest.fixture()
def live_mode(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")


def _hold(text):
    policy, registry = _policy(), _registry()
    decision = gate(text, policy, registry, mode="send")
    assert decision.decision == "HOLD", decision.reasons
    hold = holds.create_hold(
        decision, text, [{"role": "user", "content": text}], "ollama"
    )
    return hold, policy, registry, decision


def _needs_human_hold(cls):
    _clean()
    text = f"maybe {ENTROPY_TOKEN} here"
    hold, policy, registry, _decision = _hold(text)
    result = holds.run_review(
        hold.hold_id,
        judge_transport=_transport(_payload(cls, "zzz-no-such-span")),
        sender=_sender(),
        policy=policy,
        registry=registry,
    )
    assert result is not None and result.status == "needs_human"
    return hold, text


def test_approve_replaces_secret_suspicion_span(live_mode):
    hold, text = _needs_human_hold("secret")
    try:
        send = _sender("noted [SECRET_1], thanks")
        result = holds.approve_hold(hold, sender=send)
        assert result is not None
        assert hold.status == "approved"
        assert ENTROPY_TOKEN not in (result["sent_text"] or "")
        assert "[SECRET_1]" in (result["sent_text"] or "")
        assert len(send.calls) == 1
        assert ENTROPY_TOKEN not in send.calls[0]
        assert "[SECRET_1]" in send.calls[0]
    finally:
        _clean()


def test_approve_replaces_beacon_suspicion_span(live_mode):
    hold, text = _needs_human_hold("beacon")
    try:
        send = _sender("noted [BEACON_1], thanks")
        result = holds.approve_hold(hold, sender=send)
        assert result is not None
        assert ENTROPY_TOKEN not in (result["sent_text"] or "")
        assert "[BEACON_1]" in (result["sent_text"] or "")
        assert ENTROPY_TOKEN not in send.calls[0]
    finally:
        _clean()


def test_needs_human_card_title_names_class_and_confidence(live_mode):
    hold, _text = _needs_human_hold("secret")
    try:
        card = hold_status(hold.hold_id)
        assert card["title"] == (
            "Glimmer flagged a possible secret (70%). "
            "Code could not prove it, so it's your call."
        )
    finally:
        _clean()


def test_resolution_card_titles(live_mode):
    _clean()
    try:
        # Released path: model clears the span, nothing proven stays.
        text = f"maybe {ENTROPY_TOKEN}"
        hold, policy, registry, _decision = _hold(text)
        released = holds.run_review(
            hold.hold_id,
            judge_transport=_transport(
                _payload("benign", ENTROPY_TOKEN,
                         sensitivity=0.0, confidence=0.95)
            ),
            sender=_sender("all good"),
            policy=policy,
            registry=registry,
        )
        assert released is not None and released.status == "released"
        card = hold_status(hold.hold_id)
        assert card["title"] == "Glimmer checked it: nothing sensitive. Sent."

        # Redacted-sent path: code confirms the entropy span.
        _clean()
        hold2, policy2, registry2, _decision2 = _hold(text)
        redacted = holds.run_review(
            hold2.hold_id,
            judge_transport=_transport(_payload("secret", ENTROPY_TOKEN)),
            sender=_sender("noted [SECRET_1]"),
            policy=policy2,
            registry=registry2,
        )
        assert redacted is not None and redacted.status == "redacted_sent"
        card2 = hold_status(hold2.hold_id)
        assert card2["title"] == (
            "Glimmer found it and code confirmed it. "
            "Sent with the part removed."
        )
    finally:
        _clean()


def test_preview_marks_proven_and_suspicion(live_mode):
    hold, text = _needs_human_hold("secret")
    try:
        card = hold_status(hold.hold_id)
        marks = card["preview_marks"]
        kinds = {m["kind"] for m in marks}
        assert "suspicion" in kinds
        preview = card["redacted_preview"]
        for mark in marks:
            assert 0 <= mark["start"] < mark["end"]
            assert mark["end"] <= len(preview.encode("utf-16-le")) // 2
        # The suspicion mark covers the raw flagged token (UTF-16 slice).
        raw = preview.encode("utf-16-le")
        assert any(
            raw[mark["start"] * 2:mark["end"] * 2].decode("utf-16-le")
            == ENTROPY_TOKEN
            for mark in marks
            if mark["kind"] == "suspicion"
        )
    finally:
        _clean()


def test_stats_count_one_decision_per_id(live_mode):
    _clean()
    from shadowfenced import ecs_log

    text = f"maybe {ENTROPY_TOKEN}"
    hold, policy, registry, decision = _hold(text)
    assert hold.hold_id == decision.decision_id
    ecs_log.append_decision(None, decision, text)
    snap = stats_from_log()
    assert snap["total"] == 1
    assert snap["decisions"] == {"HOLD": 1}
    assert snap["held_then_resolved"] == 0

    result = holds.run_review(
        hold.hold_id,
        judge_transport=_transport(_payload("secret", ENTROPY_TOKEN)),
        sender=_sender("noted [SECRET_1]"),
        policy=policy,
        registry=registry,
    )
    assert result is not None and result.status == "redacted_sent"
    snap = stats_from_log()
    assert snap["total"] == 1
    assert snap["decisions"] == {"REDACT": 1}
    assert snap["held_then_resolved"] == 1
    _clean()


def test_hold_polish_ui_hooks():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for hook in (
        'data-testid", "file-chip"',
        "file_ids:",
        '"/api/upload"',
        '"/api/providers"',
        '"/api/provider"',
        "(no API key)",
        '"Send without flagged part"',
        '"Don\'t send"',
        '"(nothing sent yet)"',
        '"via "',
        "preview_marks",
        '"pv-suspicion"',
        '"pv-proven"',
    ):
        assert hook in js, hook
    for hook in (
        'data-testid="file-input"',
        'data-testid="provider-select"',
        'data-testid="stats-held-resolved"',
    ):
        assert hook in html, hook
    for hook in (".file-chip", ".pv-proven", ".pv-suspicion", ".answer-src"):
        assert hook in css, hook
