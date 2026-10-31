"""Tier 3 verifier tests (T9): code confirms or rejects model claims.

Every secret is built at runtime from the canary registry, and fake
provider keys are assembled from fragments, so no secret literal
appears in this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shadowfenced import tier1
from shadowfenced.canary import CanaryRegistry, encodings
from shadowfenced.judge import Judgement
from shadowfenced.policy import load_policy
from shadowfenced.verifier import Verdict, verify

SEED = "test-seed-t9"
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
    }
    payload.update(overrides)
    return Judgement.model_validate(payload)


def test_span_not_in_text_unconfirmed(policy, registry):
    text = "banana bread with walnuts and honey"
    spans = tier1.scan(text, policy, registry)
    verdict = verify(text, _judge("secret", "no such token here"), spans,
                     policy, registry)
    assert isinstance(verdict, Verdict)
    assert verdict.confirmed is False
    assert verdict.span is None
    assert "exact substring" in verdict.why


def test_empty_span_unconfirmed(policy, registry):
    text = "something"
    verdict = verify(text, _judge("secret", ""), [], policy, registry)
    assert verdict.confirmed is False
    assert verdict.span is None


def test_base64_claim_decoding_to_canary_confirmed(policy, registry):
    canary = registry.value("db_password")
    blob = encodings(canary)["base64"]
    text = f"debug log pasted {blob} by accident"
    spans = tier1.scan(text, policy, registry)
    verdict = verify(text, _judge("secret", blob), spans, policy, registry)
    assert verdict.confirmed is True
    assert verdict.cls == "secret"
    assert verdict.span is not None
    assert verdict.span.subclass == "canary"
    assert verdict.span.tier == 3
    # Offsets are code-found: slicing the text yields the span back.
    assert text[verdict.span.cp_start:verdict.span.cp_end] == blob
    assert "canary" in verdict.why


def test_identity_canary_inside_wider_span_confirmed(policy, registry):
    canary = registry.value("api_key")
    text = f"leaked value {canary} yesterday"
    spans = tier1.scan(text, policy, registry)
    verdict = verify(
        text, _judge("secret", f"value {canary} yesterday"), spans,
        policy, registry,
    )
    assert verdict.confirmed is True
    assert verdict.span is not None
    assert verdict.span.subclass == "canary"


def test_provider_shape_confirmed(policy, registry):
    key = "AK" + "IA" + "A1B2C3D4E5F60718"
    text = f"deploy with {key} in env"
    spans = tier1.scan(text, policy, registry)
    verdict = verify(text, _judge("secret", key), spans, policy, registry)
    assert verdict.confirmed is True
    assert verdict.span is not None
    assert verdict.span.subclass == "provider_key"


def test_password_assignment_confirmed(policy, registry):
    text = "login with password: hunter2value!"
    spans = tier1.scan(text, policy, registry)
    verdict = verify(
        text, _judge("secret", "password: hunter2value!"), spans,
        policy, registry,
    )
    assert verdict.confirmed is True
    assert verdict.span is not None


def test_entropy_claim_confirmed(policy, registry):
    text = f"maybe this {ENTROPY_TOKEN} is sensitive"
    spans = tier1.scan(text, policy, registry)
    assert any(s.cls == "secret" for s in spans)
    verdict = verify(
        text, _judge("secret", ENTROPY_TOKEN), spans, policy, registry
    )
    assert verdict.confirmed is True
    assert verdict.span is not None


def test_secret_without_code_evidence_unconfirmed(policy, registry):
    text = "banana bread with walnuts and honey"
    spans = tier1.scan(text, policy, registry)
    verdict = verify(
        text, _judge("secret", "banana bread"), spans, policy, registry
    )
    assert verdict.confirmed is False
    assert verdict.span is None


def test_pii_confirmed_by_regex(policy, registry):
    text = "contact ada@corvanelabs.com for access"
    spans = tier1.scan(text, policy, registry)
    verdict = verify(
        text, _judge("pii", "ada@corvanelabs.com"), spans, policy,
        registry,
    )
    assert verdict.confirmed is True
    assert verdict.cls == "pii"
    assert verdict.span is not None
    assert verdict.span.subclass == "email"
    assert text[verdict.span.cp_start:verdict.span.cp_end] == (
        "ada@corvanelabs.com"
    )


def test_pii_without_match_unconfirmed(policy, registry):
    text = "contact the front desk for access"
    spans = tier1.scan(text, policy, registry)
    verdict = verify(
        text, _judge("pii", "the front desk"), spans, policy, registry
    )
    assert verdict.confirmed is False
    assert verdict.span is None


def test_business_confirmed_by_policy_word(policy, registry):
    text = "Project Tallow slips to next quarter"
    spans = tier1.scan(text, policy, registry)
    verdict = verify(
        text, _judge("business", "Project Tallow"), spans, policy,
        registry,
    )
    assert verdict.confirmed is True
    assert verdict.cls == "business"


def test_beacon_without_value_unconfirmed(policy, registry):
    text = "we moved the old keys out of config.py last spring"
    spans = tier1.scan(text, policy, registry)
    verdict = verify(
        text,
        _judge("beacon", "moved the old keys out of config.py"),
        spans, policy, registry,
    )
    assert verdict.confirmed is False
    assert verdict.span is None
    assert "nearby" in verdict.why


def test_beacon_with_location_and_value_confirmed(policy, registry):
    text = (
        "we moved the old keys out of config.py after "
        f"rotating {ENTROPY_TOKEN} last spring"
    )
    spans = tier1.scan(text, policy, registry)
    assert any(s.cls == "secret" for s in spans)
    verdict = verify(
        text,
        _judge("beacon", "moved the old keys out of config.py"),
        spans, policy, registry,
    )
    assert verdict.confirmed is True
    assert verdict.cls == "beacon"
    assert verdict.span is not None
    assert verdict.span.subclass == "beacon_cue"


def test_benign_never_confirmed(policy, registry):
    text = "banana bread with walnuts and honey"
    spans = tier1.scan(text, policy, registry)
    verdict = verify(
        text,
        _judge("benign", "banana bread", sensitivity=0.0, confidence=0.99),
        spans, policy, registry,
    )
    assert verdict.confirmed is False
    assert verdict.span is None
