"""T63c: a proven secret BLOCKs instantly with no model call.

A message that already holds a proven secret (action BLOCK, keep denied)
must return BLOCK from Tier 1 alone and skip every model check (Glimmer
Tier 2, person search, residual review). A PII-only message must still
consult the model as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shadowfenced import engine
from shadowfenced.canary import CanaryRegistry
from shadowfenced.policy import load_policy

SEED = "test-seed-t63c"
KINDS = ("db_password", "api_key")

# Provider key shape, fragments joined at runtime so no live-key shape is
# stored here (mirrors tests/test_tier1.py).
CLOUD_KEY = "AK" + "IA" + "ABCDEFGH" + "12345678"
PII_MSG = (
    "Draft a reply to Ada Byron, ada.byron@northwind-mail.co, "
    "phone +44 20 7946 0958."
)
KEY_AND_PII_MSG = PII_MSG + f" Also deploy with {CLOUD_KEY}."


@pytest.fixture()
def policy():
    return load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")


@pytest.fixture()
def registry():
    return CanaryRegistry(SEED, KINDS)


def _recorder(calls, value=None):
    def _fn(text):
        calls.append(text)
        return value

    return _fn


@pytest.mark.parametrize("mode", ["tier1", "send"])
def test_key_and_pii_blocks_without_any_model_call(policy, registry, mode):
    person_search_calls: list = []
    residual_calls: list = []
    decision = engine.gate(
        KEY_AND_PII_MSG,
        policy,
        registry,
        mode=mode,
        residual=True,
        judge_fn=_recorder(residual_calls),
        person_search_judge_fn=_recorder(person_search_calls),
    )
    assert decision.decision == "BLOCK"
    assert decision.sent_text is None
    assert decision.placeholders == {}
    assert any(
        "model checks skipped" in reason for reason in decision.reasons
    ), decision.reasons
    assert person_search_calls == []
    assert residual_calls == []


def test_pii_only_still_calls_the_model(policy, registry):
    person_search_calls: list = []
    decision = engine.gate(
        PII_MSG,
        policy,
        registry,
        mode="tier1",
        person_search_judge_fn=_recorder(person_search_calls),
    )
    assert decision.decision != "BLOCK"
    assert person_search_calls != [], "PII-only must still consult person search"


def test_warn_level_still_runs_the_residual_check(policy, registry):
    # A detailed message keeps 2+ quasi-identifiers after generalising,
    # so the residual check still asks the judge.
    residual_calls: list = []
    decision = engine.gate(
        "Ada Byron, born 14 March 1991, Copperleaf Drive, Fairhaven",
        policy,
        registry,
        mode="tier1",
        residual=True,
        judge_fn=_recorder(residual_calls),
        person_search_judge_fn=_recorder([]),
    )
    assert decision.decision != "BLOCK"
    assert residual_calls != [], "WARN-level must still run the residual check"


def test_scan_skips_model_payload_for_proven_secret(policy, registry, monkeypatch):
    from shadowfenced import app as app_module
    from shadowfenced import reid as reid_module

    # Encyclopedic framing about a fictional person reaches the Glimmer
    # public-figure check; with a proven secret present it must not.
    model_calls: list = []
    real_public_figure = reid_module.local_glimmer_public_figure

    def _recorder(text, *args, **kwargs):
        model_calls.append(text)
        return real_public_figure(text, *args, **kwargs)

    monkeypatch.setattr(
        reid_module, "local_glimmer_public_figure", _recorder
    )
    framing = "Who was Liora Venn? Summarise the life of Liora Venn."
    body = app_module.scan(
        app_module.ScanRequest(text=f"{framing} Also deploy with {CLOUD_KEY}.")
    )
    assert any(s["subclass"] == "provider_key" for s in body["spans"])
    assert model_calls == [], "secret scan must skip the re-id model check"
    assert any(
        step.get("step") == "scan.short_circuit" for step in body["trace"]
    )

    body = app_module.scan(app_module.ScanRequest(text=framing))
    assert model_calls != [], "non-secret scan still consults the model"


def test_scan_live_mode_skips_model_suspicions_for_secret(monkeypatch):
    from shadowfenced import app as app_module
    from shadowfenced import judge as judge_module

    judge_calls: list = []
    monkeypatch.setattr(
        judge_module, "judge",
        lambda *args, **kwargs: judge_calls.append(args) or None,
    )
    monkeypatch.setenv("SF_LIVE_HINTS", "on")
    entropy = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"
    secret_text = f"config holds {CLOUD_KEY} and maybe {entropy}"
    body = app_module.scan(app_module.ScanRequest(text=secret_text, mode="live"))
    assert any(s["subclass"] == "provider_key" for s in body["spans"])
    assert judge_calls == [], "secret scan must skip live model suspicions"

    pii_text = f"mail ada@corvanelabs.com and maybe {entropy}"
    body = app_module.scan(app_module.ScanRequest(text=pii_text, mode="live"))
    assert judge_calls != [], "PII-only live scan still asks the model"


def test_ext_redact_blocks_secret_without_any_model_call(monkeypatch):
    from shadowfenced import ext_api
    from shadowfenced import person_search as person_search_module
    from shadowfenced.ext_api import RedactBody, ext_redact

    calls: list = []

    def _recorder(*args, **kwargs):
        calls.append(args[0] if args else "")
        return person_search_module.PersonSearchResult()

    monkeypatch.setattr(
        person_search_module, "check_person_search", _recorder
    )
    body = ext_redact(RedactBody(text=KEY_AND_PII_MSG), None)
    assert body["decision"] == "BLOCK"
    assert body["safe_text"] == ""
    assert calls == [], "secret redact must skip person search"

    body = ext_redact(RedactBody(text=PII_MSG), None)
    assert body["decision"] != "BLOCK"
    assert calls != [], "PII-only redact still runs person search"


def test_canary_and_pii_blocks_without_any_model_call(policy, registry):
    canary = registry.value("db_password")
    person_search_calls: list = []
    text = f"{PII_MSG} Token {canary}."
    decision = engine.gate(
        text,
        policy,
        registry,
        person_search_judge_fn=_recorder(person_search_calls),
    )
    assert decision.decision == "BLOCK"
    assert person_search_calls == []
