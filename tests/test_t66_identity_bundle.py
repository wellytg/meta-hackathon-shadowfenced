"""T66: a full identity bundle is a hard BLOCK with a clear "why" card.

Every person, place and value below is invented for the test, except the
historical figure named in the negative case.
"""

from __future__ import annotations

from pathlib import Path

from shadowfenced import engine, reid
from shadowfenced.canary import CanaryRegistry
from shadowfenced.policy import load_policy

SEED = "test-seed-t66"
KINDS = ("db_password", "api_key")
POLICY_PATH = Path(__file__).resolve().parents[1] / "policy.yaml"

A = (
    "Book a dentist appointment for Priya Ramanathan, born 3 May 1991, "
    "27 Willow Crescent, Leeds LS6 2QT, phone 07700 900461, "
    "email priya.ramanathan@northwind-mail.co."
)
B = (
    "Help me fill this form: Name Priya Ramanathan. DOB 03/05/1991. "
    "Address 27 Willow Crescent, Leeds LS6 2QT. Phone 07700 900461. "
    "Email priya.ramanathan@northwind-mail.co."
)

SIX = {"name", "dob", "street", "postcode", "phone", "email"}

WHY = (
    "Together, a name, date of birth, home address, phone number and email "
    "identify one real person and where to find them. Each is personal data; "
    "combined they enable identity theft, stalking and fraud."
)


def _policy():
    return load_policy(POLICY_PATH)


def _registry():
    return CanaryRegistry(SEED, KINDS)


def test_full_bundle_lists_exactly_six_kinds():
    for text in (A, B):
        result = reid.assess(text)
        assert result.outcome == "BLOCK"
        assert set(result.combination) == SIX
        kinds = {item["kind"] for item in reid.extract_quasi_identifiers(text)}
        assert SIX <= kinds
        assert "jobtitle" not in result.combination
        assert "employer" not in result.combination


def test_full_bundle_blocks_in_send_and_tier1_modes():
    policy, registry = _policy(), _registry()
    for text in (A, B):
        for mode in ("send", "tier1"):
            decision = engine.gate(text, policy, registry, mode=mode)
            assert decision.decision == "BLOCK"
            assert decision.sent_text is None
            assert decision.evidence["reid"]["outcome"] == "BLOCK"
            assert set(decision.evidence["reid"]["combination"]) == SIX
            assert "full-identity-bundle" in decision.rule_ids
            assert engine.user_message(decision) == WHY


def test_block_banner_is_plain_words_without_values():
    result = reid.assess(A)
    assert result.banner == WHY
    for raw in ("Priya Ramanathan", "07700", "900461", "Willow Crescent",
                "priya.ramanathan@northwind-mail.co", "27"):
        assert raw not in result.banner


def test_generalised_text_hides_every_bundle_piece():
    for text in (A, B):
        result = reid.assess(text)
        generalised = result.generalised_text
        assert generalised
        for raw in ("Priya Ramanathan", "07700", "900461", "27 Willow",
                    "priya.ramanathan@northwind-mail.co"):
            assert raw not in generalised, raw
        assert "[phone removed]" in generalised
        assert "[name removed]" in generalised
        assert "a street in Leeds" in generalised
        assert "1991" in generalised


def test_name_plus_email_only_stays_warn_or_hold():
    result = reid.assess("Priya Ramanathan, priya.ramanathan@northwind-mail.co.")
    assert result.outcome in ("WARN", "HOLD")
    assert result.outcome != "BLOCK"
    decision = engine.gate(
        "Priya Ramanathan, priya.ramanathan@northwind-mail.co.",
        _policy(), _registry(), mode="send")
    assert decision.decision != "BLOCK"
    assert (decision.evidence or {}).get("reid", {}).get("outcome") in ("WARN", "HOLD")


def test_historical_figure_is_not_blocked():
    text = "Ada Lovelace, born 10 December 1815 in London."
    assert reid.assess(text).outcome == "OK"
    decision = engine.gate(text, _policy(), _registry(), mode="send")
    assert decision.decision != "BLOCK"


def test_rule_catalog_has_full_identity_bundle():
    from shadowfenced import rule_catalog as catalog_module

    entries = {entry["id"]: entry for entry in catalog_module.get_catalog(_policy())}
    entry = entries["full-identity-bundle"]
    assert entry["title"] == "Full identity bundle"
    assert entry["action"] == "BLOCK"
    assert entry["can_send_anyway"] is False
    blob = " ".join([entry["why"], *entry["related"]])
    assert "GDPR" in blob
    assert "UK GDPR" in blob
    assert "identity" in blob.lower()


def test_ext_redact_block_carries_combination_and_notes():
    from shadowfenced import ext_api

    decision = engine.gate(A, _policy(), _registry(), mode="tier1")
    assert decision.decision == "BLOCK"
    payload_notes = engine.user_message(decision)
    assert payload_notes == WHY
    assert set((decision.evidence or {})["reid"]["combination"]) == SIX
