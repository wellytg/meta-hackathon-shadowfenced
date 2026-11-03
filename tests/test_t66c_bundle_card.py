"""T66c: identity-bundle BLOCK card is readable on screen and instant.

The Jordan Ellery form message holds a full identity bundle (name, date
of birth, street address, postcode, phone, email), so Tier 1 code alone
must return BLOCK without any Glimmer call -- in ``engine.gate`` (both
``tier1`` and ``send`` modes, i.e. the ``/api/send`` path), in
``/api/scan`` live mode, and in the extension ``/api/ext/redact`` path --
exactly as T63c did for proven secrets. A lookup message with no bundle
must still consult the model. The BLOCK card shows the found pieces as
separate chips under a "Found together:" label, the why line in full,
and a plain nothing-sent line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shadowfenced import engine
from shadowfenced.canary import CanaryRegistry
from shadowfenced.policy import load_policy

SEED = "test-seed-t66c"
KINDS = ("db_password", "api_key")
ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "policy.yaml"
STATIC = ROOT / "src" / "shadowfenced" / "static"

JORDAN = (
    "Help me fill in this form. Name: Jordan Ellery. "
    "Date of birth: 12/08/1985. Address: 14 Birch Road, "
    "Manchester M14 5RT. Phone: 07700 900812. "
    "Email: jordan.ellery@brightpost.net"
)
SIX = {"name", "dob", "street", "postcode", "phone", "email"}
WHY = (
    "Together, a name, date of birth, home address, phone number and email "
    "identify one real person and where to find them. Each is personal data; "
    "combined they enable identity theft, stalking and fraud."
)
# A lookup with no bundle: still consults the model, never BLOCKs.
LOOKUP = "Search for Priya Ramanathan and her phone number"


@pytest.fixture()
def policy():
    return load_policy(POLICY_PATH)


@pytest.fixture()
def registry():
    return CanaryRegistry(SEED, KINDS)


def _recorder(calls, value=None):
    def _fn(text):
        calls.append(text)
        return value

    return _fn


@pytest.mark.parametrize("mode", ["tier1", "send"])
def test_bundle_blocks_without_any_model_call(policy, registry, mode):
    person_search_calls: list = []
    residual_calls: list = []
    decision = engine.gate(
        JORDAN,
        policy,
        registry,
        mode=mode,
        residual=True,
        judge_fn=_recorder(residual_calls),
        person_search_judge_fn=_recorder(person_search_calls),
    )
    assert decision.decision == "BLOCK"
    assert decision.sent_text is None
    assert decision.evidence["reid"]["outcome"] == "BLOCK"
    assert set(decision.evidence["reid"]["combination"]) == SIX
    assert "full-identity-bundle" in decision.rule_ids
    assert engine.user_message(decision) == WHY
    assert any(
        "model checks skipped" in reason for reason in decision.reasons
    ), decision.reasons
    assert person_search_calls == []
    assert residual_calls == []


def test_lookup_without_bundle_still_consults_the_model(policy, registry):
    person_search_calls: list = []
    decision = engine.gate(
        LOOKUP,
        policy,
        registry,
        mode="send",
        person_search_judge_fn=_recorder(person_search_calls),
    )
    assert decision.decision != "BLOCK"
    assert person_search_calls != [], "non-bundle must still consult person search"


def test_scan_live_mode_skips_glimmer_for_bundle(monkeypatch):
    from shadowfenced import app as app_module
    from shadowfenced import judge as judge_module
    from shadowfenced import reid as reid_module

    judge_calls: list = []
    monkeypatch.setattr(
        judge_module, "judge",
        lambda *args, **kwargs: judge_calls.append(args) or None,
    )
    public_figure_calls: list = []
    real_public_figure = reid_module.local_glimmer_public_figure

    def _recorder(text, *args, **kwargs):
        public_figure_calls.append(text)
        return real_public_figure(text, *args, **kwargs)

    monkeypatch.setattr(
        reid_module, "local_glimmer_public_figure", _recorder
    )
    monkeypatch.setenv("SF_LIVE_HINTS", "on")
    body = app_module.scan(app_module.ScanRequest(text=JORDAN, mode="live"))
    assert body["reid"]["outcome"] == "BLOCK"
    assert set(body["reid"]["combination"]) == SIX
    assert judge_calls == [], "bundle scan must skip live model suspicions"
    assert public_figure_calls == [], "bundle scan must skip the re-id model check"
    assert any(
        step.get("step") == "scan.short_circuit" for step in body["trace"]
    )

    entropy = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"
    pii_text = f"mail ada@corvanelabs.com and maybe {entropy}"
    body = app_module.scan(app_module.ScanRequest(text=pii_text, mode="live"))
    assert judge_calls != [], "non-bundle live scan still asks the model"


def test_ext_redact_blocks_bundle_without_any_model_call(monkeypatch):
    from shadowfenced import person_search as person_search_module
    from shadowfenced.ext_api import RedactBody, ext_redact

    calls: list = []

    def _recorder(*args, **kwargs):
        calls.append(args[0] if args else "")
        return person_search_module.PersonSearchResult()

    monkeypatch.setattr(
        person_search_module, "check_person_search", _recorder
    )
    body = ext_redact(RedactBody(text=JORDAN), None)
    assert body["decision"] == "BLOCK"
    assert body["safe_text"] == ""
    assert set(body["combination"]) == SIX
    assert body["generalised_text"]
    assert body["notes"] == WHY
    assert calls == [], "bundle redact must skip person search"

    body = ext_redact(RedactBody(text=LOOKUP), None)
    assert body["decision"] != "BLOCK"
    assert calls != [], "non-bundle redact still runs person search"


def test_block_card_chips_are_separate_labelled_elements():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '"Found together:"' in js
    assert 'chip.className = "risk-chip"' in js
    assert 'chips.setAttribute("data-testid", "risk-chips")' in js
    assert 'chip.setAttribute("data-testid", "risk-chip")' in js
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".risk-chips { display: flex; flex-wrap: wrap;" in css
    assert ".risk-chip {" in css
    assert "border: 2px solid var(--line);" in css
    assert "border-radius: 0;" in css


def test_block_card_why_shows_in_full_without_truncation():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    rule = '.risk-card[data-decision="BLOCK"] .risk-row-why .risk-value {'
    assert rule in css
    block = css.split(rule, 1)[1].split("}", 1)[0]
    assert "white-space: normal;" in block
    assert "overflow: visible;" in block
    assert "ellipsis" not in block


def test_block_card_sending_line_stays_on_this_pc():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '"Nothing. Your message stayed on this PC."' in js
    assert 'model.decision === "BLOCK"' in js
