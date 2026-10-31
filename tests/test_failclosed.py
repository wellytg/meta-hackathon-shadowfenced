"""Fail-closed tests (T9+T10+T16b): every tier raising holds; send never waits.

``engine.gate(mode="send")`` returns HOLD instantly for ambiguous
spans or sensitivity cue words without calling the judge (Glimmer
reviews the whole message later in ``holds.start_review``). The
model never overrules code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shadowfenced import tier1
from shadowfenced.engine import gate
from shadowfenced.policy import load_policy
from shadowfenced.canary import CanaryRegistry

SEED = "test-seed-failclosed"
KINDS = ("db_password", "api_key")
ENTROPY_TOKEN = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"


@pytest.fixture()
def policy():
    return load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")


@pytest.fixture()
def registry():
    return CanaryRegistry(SEED, KINDS)


def _counting_judge(monkeypatch):
    """Stub the judge with a fake that explodes when called."""
    from shadowfenced import judge as judge_module

    calls: list = []

    def fake(window, model, *, live, transport=None):
        calls.append(window)
        raise AssertionError("send path must not call the judge")

    monkeypatch.setattr(judge_module, "judge", fake)
    return calls


def test_tier1_raising_holds_in_send_mode(policy, registry, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("tier1 down")

    monkeypatch.setattr(tier1, "scan", _boom)
    decision = gate("hello world", policy, registry, mode="send")
    assert decision.decision == "HOLD"
    assert decision.sent_text is None
    assert decision.resolved_tier == 4
    assert set(decision.latency_ms) == {"tier1", "tier2", "tier3", "total"}
    assert any("internal_error" in reason for reason in decision.reasons)


def _pending_by_kind(decision):
    return {
        item["kind"]: item
        for item in decision.evidence["pending"]
        if isinstance(item, dict)
    }


def test_send_hold_fast_without_calling_judge(
    policy, registry, monkeypatch
):
    calls = _counting_judge(monkeypatch)
    text = f"maybe {ENTROPY_TOKEN}"
    decision = gate(text, policy, registry, mode="send")
    assert decision.decision == "HOLD"
    assert decision.sent_text is None
    assert decision.resolved_tier == 4
    assert decision.status == "reviewing"
    pending = _pending_by_kind(decision)
    assert pending["message"]["text"] == text
    assert pending["span"]["text"] == ENTROPY_TOKEN
    assert decision.evidence["proven_by_code"] == []
    assert calls == []


def test_send_hold_keeps_proven_ids_and_pending_texts(
    policy, registry, monkeypatch
):
    calls = _counting_judge(monkeypatch)
    text = f"mail ada@corvanelabs.com and {ENTROPY_TOKEN}"
    decision = gate(text, policy, registry, mode="send")
    assert decision.decision == "HOLD"
    assert decision.sent_text is None
    pending = _pending_by_kind(decision)
    assert pending["message"]["text"] == text
    assert pending["span"]["text"] == ENTROPY_TOKEN
    assert len(decision.evidence["proven_by_code"]) == 1
    assert calls == []


def test_send_cue_word_holds_with_pending_message_item(
    policy, registry, monkeypatch
):
    calls = _counting_judge(monkeypatch)
    text = "the vault audit is scheduled"
    decision = gate(text, policy, registry, mode="send")
    assert decision.decision == "HOLD"
    assert decision.sent_text is None
    assert decision.resolved_tier == 4
    assert decision.status == "reviewing"
    pending = _pending_by_kind(decision)
    assert pending["message"]["text"] == text
    assert "span" not in pending
    assert decision.evidence["proven_by_code"] == []
    assert calls == []


def test_send_cue_word_hold_truncates_message_item(
    policy, registry, monkeypatch
):
    calls = _counting_judge(monkeypatch)
    text = "vault " + "x" * 3000
    decision = gate(text, policy, registry, mode="send")
    assert decision.decision == "HOLD"
    pending = _pending_by_kind(decision)
    assert pending["message"]["text"] == text[:2000]
    assert calls == []


def test_send_redact_proven_without_judge(policy, registry, monkeypatch):
    calls = _counting_judge(monkeypatch)
    decision = gate(
        "mail ada@corvanelabs.com now", policy, registry, mode="send"
    )
    assert decision.decision == "REDACT"
    assert "[PII_EMAIL_1]" in (decision.sent_text or "")
    assert calls == []


def test_send_allow_clean_without_judge(policy, registry, monkeypatch):
    calls = _counting_judge(monkeypatch)
    decision = gate("hello world", policy, registry, mode="send")
    assert decision.decision == "ALLOW"
    assert decision.sent_text == "hello world"
    assert calls == []


def test_send_proven_canary_blocks_without_judge(
    policy, registry, monkeypatch
):
    calls = _counting_judge(monkeypatch)
    canary = registry.value("db_password")
    decision = gate(f"password: {canary}", policy, registry, mode="send")
    assert decision.decision == "BLOCK"
    assert decision.sent_text is None
    assert calls == []


def test_model_benign_cannot_clear_synchronously(
    policy, registry, monkeypatch
):
    """Even a confident benign verdict must wait for async review."""
    calls = _counting_judge(monkeypatch)
    text = f"mail ada@corvanelabs.com and {ENTROPY_TOKEN}"
    decision = gate(text, policy, registry, mode="send")
    assert decision.decision == "HOLD"
    assert decision.status == "reviewing"
    assert calls == []
