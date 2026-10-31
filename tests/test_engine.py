"""Engine tests (T4): Decision shape, severity, choices, fail-closed.

Every secret is built at runtime from the canary registry, so no
secret literal appears in this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shadowfenced import tier1
from shadowfenced.canary import CanaryRegistry
from shadowfenced.engine import Decision, gate
from shadowfenced.policy import load_policy

SEED = "test-seed-t4"
KINDS = ("db_password", "api_key")
ENTROPY_TOKEN = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"


@pytest.fixture()
def policy():
    return load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")


@pytest.fixture()
def registry():
    return CanaryRegistry(SEED, KINDS)


def test_decision_shape_allow(policy, registry):
    decision = gate("hello world, nothing sensitive", policy, registry)
    assert isinstance(decision, Decision)
    assert decision.decision == "ALLOW"
    assert len(decision.decision_id) == 32
    assert int(decision.decision_id, 16) >= 0
    assert decision.spans == []
    assert decision.sent_text == "hello world, nothing sensitive"
    assert decision.placeholders == {}
    assert decision.resolved_tier == 1
    assert set(decision.latency_ms) == {"tier1", "total"}
    assert decision.reasons


def test_gate_canary_blocks(policy, registry):
    canary = registry.value("db_password")
    decision = gate(f"password: {canary}", policy, registry)
    assert decision.decision == "BLOCK"
    assert decision.sent_text is None
    assert len(decision.spans) == 1
    assert decision.spans[0].subclass == "canary"


def test_gate_proven_pii_redacts(policy, registry):
    decision = gate("mail ada@corvanelabs.com now", policy, registry)
    assert decision.decision == "REDACT"
    assert decision.sent_text is not None
    assert "ada@corvanelabs.com" not in decision.sent_text
    assert "[PII_EMAIL_1]" in decision.sent_text
    assert decision.placeholders == {"[PII_EMAIL_1]": "ada@corvanelabs.com"}


def test_gate_proven_business_redacts(policy, registry):
    decision = gate("Project Tallow slips to Q3", policy, registry)
    assert decision.decision == "REDACT"
    assert decision.sent_text is not None
    assert "[CODENAME_1]" in decision.sent_text
    assert "Tallow" not in decision.sent_text


def test_gate_ambiguous_holds_for_tier2(policy, registry):
    decision = gate(f"here is {ENTROPY_TOKEN} maybe", policy, registry)
    assert decision.decision == "HOLD"
    assert decision.sent_text is None
    assert any(s.status == "ambiguous" for s in decision.spans)


def test_gate_block_outranks_hold(policy, registry):
    canary = registry.value("db_password")
    decision = gate(f"{canary} plus {ENTROPY_TOKEN}", policy, registry)
    assert decision.decision == "BLOCK"


def test_gate_hold_outranks_redact(policy, registry):
    decision = gate(
        f"mail ada@corvanelabs.com and {ENTROPY_TOKEN}", policy, registry
    )
    assert decision.decision == "HOLD"
    assert decision.sent_text is None


def test_choices_keep_allowed_class(policy, registry):
    text = "mail ada@corvanelabs.com now"
    spans = tier1.scan(text, policy, registry)
    span_id = next(s.span_id for s in spans if s.subclass == "email")
    decision = gate(
        text, policy, registry, [{"span_id": span_id, "choice": "keep"}]
    )
    assert decision.decision == "ALLOW"
    assert decision.sent_text == text
    assert any("kept" in reason for reason in decision.reasons)


def test_choices_keep_never_for_secret(policy, registry):
    canary = registry.value("db_password")
    text = f"password: {canary}"
    spans = tier1.scan(text, policy, registry)
    assert len(spans) == 1
    decision = gate(
        text, policy, registry, [{"span_id": spans[0].span_id, "choice": "keep"}]
    )
    assert decision.decision == "BLOCK"
    assert decision.sent_text is None
    assert any("denied" in reason for reason in decision.reasons)


def test_choices_unknown_span_logged(policy, registry):
    decision = gate(
        "hello world", policy, registry, [{"span_id": "nope", "choice": "keep"}]
    )
    assert decision.decision == "ALLOW"
    assert any("unknown span" in reason for reason in decision.reasons)


def test_gate_internal_error_holds(policy, registry, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("tier1 down")

    monkeypatch.setattr(tier1, "scan", _boom)
    decision = gate("hello world", policy, registry)
    assert decision.decision == "HOLD"
    assert decision.sent_text is None
    assert any("internal_error" in reason for reason in decision.reasons)


def test_decision_ids_unique(policy, registry):
    first = gate("hello", policy, registry)
    second = gate("hello", policy, registry)
    assert first.decision_id != second.decision_id
