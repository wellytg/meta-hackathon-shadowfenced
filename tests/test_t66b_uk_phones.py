"""T66b: Tier 1 detects UK and international phone numbers as PII_PHONE.

UK landlines/mobiles (spaced or contiguous) and E.164 numbers with spaces
or dashes redact to ``[PII_PHONE_n]`` exactly like US phones. Years,
dates, sort codes, card-like runs, SHAs, UUIDs, versions, prices,
postcodes, labelled order numbers and short PINs never flag as phones.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shadowfenced import engine, tier1
from shadowfenced.canary import CanaryRegistry
from shadowfenced.policy import load_policy
from shadowfenced.redact import redact

POLICY_PATH = Path(__file__).resolve().parents[1] / "policy.yaml"
SEED = "test-seed-t66b"
KINDS = ("db_password", "api_key")


def _policy():
    return load_policy(POLICY_PATH)


def _registry():
    return CanaryRegistry(SEED, KINDS)


def _phones(text):
    return [
        s for s in tier1.scan(text, _policy(), _registry())
        if s.subclass == "phone"
    ]


# Every format the task requires to be detected. Neutral wording on
# purpose: no cue word (phone/tel/call/...) props these up.
DETECTED = [
    "0161 496 0732",
    "020 7946 0958",
    "01632 960961",
    "07700 900461",
    "07700900461",
    "+44 161 496 0732",
    "+44 (0)20 7946 0958",
    "+1 415 555 0132",
    "+49 30 901820",
    "+61 2 9374 4000",
]


@pytest.mark.parametrize("number", DETECTED)
def test_uk_and_international_numbers_are_phones(number):
    hits = _phones(f"Reach me at {number} tomorrow.")
    assert len(hits) == 1, f"no single phone span for {number!r}"
    assert hits[0].status == "proven"
    assert hits[0].suggested_action == "redact"


def test_full_uk_number_is_one_span():
    text = "her number is 0161 496 0732 today"
    hits = _phones(text)
    assert len(hits) == 1
    assert text[hits[0].cp_start:hits[0].cp_end] == "0161 496 0732"


def test_detected_numbers_redact_to_pii_phone():
    text = "her number is 0161 496 0732 today"
    spans = tier1.scan(text, _policy(), _registry())
    redacted, _ = redact(text, spans)
    assert "[PII_PHONE_1]" in redacted
    assert "0161 496 0732" not in redacted


def test_live_sentence_redacts_phone_and_email():
    text = (
        "Draft a reply to Mara Voss, her number is 0161 496 0732 "
        "and email mara.voss@brightpost.net, saying the meeting moved."
    )
    decision = engine.gate(text, _policy(), _registry(), mode="send")
    assert decision.sent_text is not None
    assert "0161 496 0732" not in decision.sent_text
    assert "mara.voss@brightpost.net" not in decision.sent_text
    assert "[PII_PHONE_1]" in decision.sent_text
    assert "[PII_EMAIL_1]" in decision.sent_text


# Each false-positive class: a realistic sentence that must yield no
# phone span (other classes such as dob or sort_code may still fire).
BENIGN = [
    "Ada Lovelace, born 10 December 1815 in London.",  # year
    "DOB 03/05/1991 on the form.",  # date with slashes
    "Shipped on 2026-11-02 by courier.",  # ISO date
    "Sort code 20-00-00 for the transfer.",  # UK sort code
    "card 4111 1111 1111 1111 charged",  # card (card detector owns it)
    "commit da39a3ee5e6b4b0d3255bfef95601890afd80799 done",  # git SHA
    "run 123e4567-e89b-12d3-a456-426614174000 done",  # UUID
    "needs version 1.4.14 or later",  # version string
    "The total is £1,250.00 due Friday.",  # price
    "Postcode LS6 2QT for delivery.",  # postcode
    "Order 1234567 shipped today.",  # explicitly labelled order number
    "Your PIN is 1234.",  # 4-digit PIN
]


@pytest.mark.parametrize("text", BENIGN)
def test_benign_numbers_are_not_phones(text):
    assert _phones(text) == [], f"false phone hit in {text!r}"
