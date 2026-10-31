"""Fail-closed tests (T9): every tier raising holds; the model never
overrules code.

The judge is stubbed (no network); the verifier runs for real unless a
test forces it to raise.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from shadowfenced import engine, tier1
from shadowfenced.canary import CanaryRegistry, encodings
from shadowfenced.engine import gate
from shadowfenced.judge import Judgement
from shadowfenced.policy import load_policy
from shadowfenced.spans import span_from_cp_range

SEED = "test-seed-failclosed"
KINDS = ("db_password", "api_key")
ENTROPY_TOKEN = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"


@pytest.fixture()
def policy():
    return load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")


@pytest.fixture()
def registry():
    return CanaryRegistry(SEED, KINDS)


def _judge(cls, span, **overrides):
    payload = {
        "cls": cls,
        "sensitivity": 0.8,
        "is_beacon": cls == "beacon",
        "span": span,
        "confidence": 0.7,
        "reason": "stubbed claim",
    }
    payload.update(overrides)
    return Judgement.model_validate(payload)


def _stub_judge(monkeypatch, judgement_or_error):
    from shadowfenced import judge as judge_module

    def fake(window, model, *, live, transport=None):
        if isinstance(judgement_or_error, BaseException):
            raise judgement_or_error
        return judgement_or_error

    monkeypatch.setattr(judge_module, "judge", fake)


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


def test_judge_raising_holds(policy, registry, monkeypatch):
    _stub_judge(monkeypatch, RuntimeError("judge down"))
    decision = gate(f"maybe {ENTROPY_TOKEN}", policy, registry, mode="send")
    assert decision.decision == "HOLD"
    assert decision.sent_text is None
    assert decision.resolved_tier == 4
    assert any("judge unavailable" in reason for reason in decision.reasons)
    assert decision.evidence["model_suspicion_only"][0]["judgement"] is None


def test_judge_timeout_holds(policy, registry, monkeypatch):
    _stub_judge(monkeypatch, httpx.ConnectTimeout("slow model"))
    decision = gate(f"maybe {ENTROPY_TOKEN}", policy, registry, mode="send")
    assert decision.decision == "HOLD"
    assert decision.resolved_tier == 4
    assert any("judge unavailable" in reason for reason in decision.reasons)


def test_verifier_raising_holds(policy, registry, monkeypatch):
    from shadowfenced import verifier as verifier_module

    _stub_judge(monkeypatch, _judge("secret", ENTROPY_TOKEN))

    def _boom(*args, **kwargs):
        raise RuntimeError("verifier down")

    monkeypatch.setattr(verifier_module, "verify", _boom)
    decision = gate(f"maybe {ENTROPY_TOKEN}", policy, registry, mode="send")
    assert decision.decision == "HOLD"
    assert decision.resolved_tier == 4
    assert any("verification failed" in reason for reason in decision.reasons)


def test_model_clean_cannot_unblock_tier1(policy, registry, monkeypatch):
    _stub_judge(
        monkeypatch,
        _judge("benign", ENTROPY_TOKEN, sensitivity=0.0, confidence=0.99),
    )
    text = f"mail ada@corvanelabs.com and maybe {ENTROPY_TOKEN}"
    decision = gate(text, policy, registry, mode="send")
    assert decision.decision == "REDACT"
    assert "ada@corvanelabs.com" not in (decision.sent_text or "")
    assert "[PII_EMAIL_1]" in (decision.sent_text or "")


def test_low_confidence_clean_holds(policy, registry, monkeypatch):
    _stub_judge(
        monkeypatch,
        _judge("benign", ENTROPY_TOKEN, sensitivity=0.1, confidence=0.5),
    )
    decision = gate(f"maybe {ENTROPY_TOKEN}", policy, registry, mode="send")
    assert decision.decision == "HOLD"
    assert decision.resolved_tier == 4


def test_high_sensitivity_clean_holds(policy, registry, monkeypatch):
    _stub_judge(
        monkeypatch,
        _judge("benign", ENTROPY_TOKEN, sensitivity=0.5, confidence=0.99),
    )
    decision = gate(f"maybe {ENTROPY_TOKEN}", policy, registry, mode="send")
    assert decision.decision == "HOLD"
    assert decision.resolved_tier == 4


def test_confident_clean_allows(policy, registry, monkeypatch):
    _stub_judge(
        monkeypatch,
        _judge("benign", ENTROPY_TOKEN, sensitivity=0.0, confidence=0.9),
    )
    text = f"maybe {ENTROPY_TOKEN}"
    decision = gate(text, policy, registry, mode="send")
    assert decision.decision == "ALLOW"
    assert decision.sent_text == text
    assert decision.resolved_tier == 3


def test_unverified_claim_holds_with_evidence(policy, registry, monkeypatch):
    _stub_judge(monkeypatch, _judge("secret", "banana"))
    text = f"banana {ENTROPY_TOKEN} split"
    decision = gate(text, policy, registry, mode="send")
    assert decision.decision == "HOLD"
    assert decision.resolved_tier == 4
    assert "proven_by_code" in decision.evidence
    suspicion = decision.evidence["model_suspicion_only"][0]
    assert suspicion["judgement"]["cls"] == "secret"
    assert suspicion["why_unverified"]


def test_confirmed_secret_redacts_at_tier3(policy, registry, monkeypatch):
    _stub_judge(monkeypatch, _judge("secret", ENTROPY_TOKEN))
    text = f"maybe {ENTROPY_TOKEN}"
    decision = gate(text, policy, registry, mode="send")
    assert decision.decision == "REDACT"
    assert decision.resolved_tier == 3
    assert ENTROPY_TOKEN not in (decision.sent_text or "")
    assert set(decision.latency_ms) == {"tier1", "tier2", "tier3", "total"}


def test_model_confirmed_canary_blocks_at_tier3(
    policy, registry, monkeypatch
):
    blob = encodings(registry.value("db_password"))["base64"]
    text = f"blob {blob} here"
    start = text.find(blob)

    def _scan_only_ambiguous(text_arg, policy_arg, registry_arg):
        return [
            span_from_cp_range(
                text_arg, start, start + len(blob),
                cls="secret", subclass="entropy", status="ambiguous",
                message="stub", why="stub", suggested_action="review",
                can_keep=False, confidence=0.5,
            )
        ]

    monkeypatch.setattr(tier1, "scan", _scan_only_ambiguous)
    _stub_judge(monkeypatch, _judge("secret", blob))
    decision = gate(text, policy, registry, mode="send")
    assert decision.decision == "BLOCK"
    assert decision.resolved_tier == 3
    assert decision.sent_text is None
