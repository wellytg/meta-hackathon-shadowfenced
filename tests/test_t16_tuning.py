"""T16 tuning tests: send-time cue HOLD plus general Tier 1 fixes.

Every example below is invented for this file (fresh names, streets,
numbers); none is copied from any corpus file.
"""

from __future__ import annotations

import base64
import random
from pathlib import Path

import pytest

from shadowfenced import engine, tier1
from shadowfenced.canary import CanaryRegistry
from shadowfenced.config import get_settings
from shadowfenced.policy import load_policy

SEED = "test-seed-t16"
KINDS = ("db_password", "api_key")


@pytest.fixture()
def policy():
    return load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")


@pytest.fixture()
def registry():
    return CanaryRegistry(SEED, KINDS)


# --- live-hints flag ---------------------------------------------------------


def test_live_hints_default_off():
    assert get_settings({}).live_hints is False


def test_live_hints_on_parsing():
    assert get_settings({"SF_LIVE_HINTS": "on"}).live_hints is True
    assert get_settings({"SF_LIVE_HINTS": "off"}).live_hints is False


# --- engine send cue HOLD (no model call) ------------------------------------


def test_cue_word_holds_without_model_call(policy, registry, monkeypatch):
    from shadowfenced import judge as judge_module

    calls: list = []

    def fake(window, model, *, live, transport=None):
        calls.append(window)
        raise AssertionError("send path must not call the judge")

    monkeypatch.setattr(judge_module, "judge", fake)
    text = "the vault audit is scheduled"
    assert tier1.scan(text, policy, registry) == []
    decision = engine.gate(text, policy, registry, mode="send")
    assert decision.decision == "HOLD"
    assert decision.resolved_tier == 4
    assert decision.status == "reviewing"
    pending = {item["kind"]: item for item in decision.evidence["pending"]}
    assert pending["message"]["text"] == text
    assert calls == []


def test_sensitivity_cue_words():
    assert engine.has_sensitivity_cue("reset the vault password")
    assert engine.has_sensitivity_cue("share is at \\nas01\\archive")
    assert engine.has_sensitivity_cue("her date of birth is unknown")
    assert not engine.has_sensitivity_cue("hello world")


# --- PII: names in context ---------------------------------------------------


@pytest.mark.parametrize(
    "text,name",
    [
        ("my name is Rafael Ortega", "Rafael Ortega"),
        ("Name: Lena Fischer", "Lena Fischer"),
        ("From: Tomas Herrera", "Tomas Herrera"),
        ("me llamo Diego Fuentes", "Diego Fuentes"),
        ("soy Lucia Prada", "Lucia Prada"),
        ("Dear Ana Beltran, welcome", "Ana Beltran"),
        ("my name is marco Polo", "marco Polo"),
        ("patient: Nora Quintero", "Nora Quintero"),
    ],
)
def test_names_in_context_proven(policy, registry, text, name):
    spans = tier1.scan(text, policy, registry)
    hit = [
        s for s in spans
        if s.cls == "pii" and s.subclass == "name" and s.status == "proven"
    ]
    assert hit, f"no proven name span in {text!r}"
    assert text[hit[0].cp_start:hit[0].cp_end] == name


def test_name_cue_without_two_tokens_clean(policy, registry):
    assert tier1.scan("Dear team, hello", policy, registry) == []
    assert tier1.scan("dear santa", policy, registry) == []


# --- PII: CSV awareness -------------------------------------------------------


def test_csv_values_under_sensitive_headers(policy, registry):
    text = (
        "full_name,email,phone\n"
        "Rafael Ortega,rafael@northwind-mail.com,555-0142\n"
        "Lena Fischer,lena@northwind-mail.com,555-0199"
    )
    spans = tier1.scan(text, policy, registry)
    by_sub = {s.subclass for s in spans if s.status == "proven"}
    assert {"name", "email", "phone"} <= by_sub
    redacted = sorted(
        text[s.cp_start:s.cp_end] for s in spans if s.status == "proven"
    )
    assert "Rafael Ortega" in redacted
    assert "555-0142" in redacted


def test_csv_non_sensitive_column_ignored(policy, registry):
    text = "city,zip\nSpringfield,90210"
    assert tier1.scan(text, policy, registry) == []


# --- PII: dates of birth ------------------------------------------------------


@pytest.mark.parametrize(
    "text,date",
    [
        ("born 04/11/1978 in Reno", "04/11/1978"),
        ("dob: 1988-02-29", "1988-02-29"),
        ("birth 3.7.1990 on record", "3.7.1990"),
        ("fecha de nacimiento 12/05/1981", "12/05/1981"),
    ],
)
def test_dob_near_cue_proven(policy, registry, text, date):
    spans = tier1.scan(text, policy, registry)
    hit = [
        s for s in spans
        if s.subclass == "dob" and s.status == "proven"
    ]
    assert hit, f"no proven dob span in {text!r}"
    assert text[hit[0].cp_start:hit[0].cp_end] == date


def test_date_without_cue_clean(policy, registry):
    assert tier1.scan("meeting on 04/11/1978", policy, registry) == []


# --- PII: street addresses ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ship to 782 Maple Grove Ave Apt 4",
        "visit 9 Cedar Ln, Reno NV 89501",
        "office at Calle Verde 21",
        "Via Roma 7 is the venue",
    ],
)
def test_street_addresses_proven(policy, registry, text):
    spans = tier1.scan(text, policy, registry)
    assert any(
        s.subclass == "address" and s.status == "proven" for s in spans
    ), f"no proven address span in {text!r}"


def test_address_lookalikes_clean(policy, registry):
    assert tier1.scan("12 Dr Who is a show", policy, registry) == []
    assert tier1.scan("100 ways to cook", policy, registry) == []


# --- PII: phones --------------------------------------------------------------


def test_short_number_with_phone_cue_proven(policy, registry):
    spans = tier1.scan("call me at 555-0142", policy, registry)
    assert any(
        s.subclass == "phone" and s.status == "proven" for s in spans
    )


def test_short_number_without_context_clean(policy, registry):
    assert tier1.scan("ticket 555-0142 closed", policy, registry) == []


def test_paren_area_code_proven(policy, registry):
    spans = tier1.scan("ring (312) 555-0187 now", policy, registry)
    assert any(
        s.subclass == "phone" and s.status == "proven" for s in spans
    )


def test_luhn_card_proven(policy, registry):
    spans = tier1.scan("charge 4539 1488 0343 6467 today", policy, registry)
    assert any(
        s.subclass == "card" and s.status == "proven" for s in spans
    )


# --- business: fuzzy clients --------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Bexley Marne signed today",
        "Aster Freigth delayed",
    ],
)
def test_fuzzy_client_names_proven(policy, registry, text):
    spans = tier1.scan(text, policy, registry)
    assert any(
        s.subclass == "client" and s.status == "proven" for s in spans
    ), f"no proven client span in {text!r}"


def test_far_client_typos_clean(policy, registry):
    assert tier1.scan("Astar Freight renewed", policy, registry) == []
    assert tier1.scan("Bexley Morning parade", policy, registry) == []


# --- business: codenames + figures --------------------------------------------


def test_lowercase_codename_with_business_context(policy, registry):
    spans = tier1.scan("the tallow launch slipped again", policy, registry)
    assert any(
        s.subclass == "codename" and s.status == "proven" for s in spans
    )


def test_codename_suppressed_by_cooking_words(policy, registry):
    text = "the tallow release for our candle recipe"
    assert tier1.scan(text, policy, registry) == []


def test_figures_in_business_sentence(policy, registry):
    text = "Bexley Marine deal closed at 12% growth"
    spans = tier1.scan(text, policy, registry)
    assert any(
        s.subclass == "client" and s.status == "proven" for s in spans
    )
    figures = [
        s for s in spans if s.subclass == "figure" and s.status == "proven"
    ]
    assert figures
    assert any(
        "12%" in text[s.cp_start:s.cp_end] for s in figures
    ), [text[s.cp_start:s.cp_end] for s in figures]


def test_amount_without_business_span_clean(policy, registry):
    assert tier1.scan("growth of 12% this quarter", policy, registry) == []


# --- false alarms -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "password: changeme",
        "token=xxx",
        "secret: <redacted>",
        "password: ${DB_PASS}",
        "token: {{api_key}}",
        "password: your_temp_value",
        "secret: REPLACE_ME",
        "password: ***",
        "token: test",
    ],
)
def test_password_placeholders_ignored(policy, registry, text):
    assert tier1.scan(text, policy, registry) == []


def test_real_password_still_proven(policy, registry):
    spans = tier1.scan("password: r4v3n-cl4w!", policy, registry)
    assert any(
        s.subclass == "password_assignment" and s.status == "proven"
        for s in spans
    )


@pytest.mark.parametrize("magic", [b"\x89PNG\r\n\x1a\n", b"GIF89a"])
def test_file_magic_base64_not_ambiguous(policy, registry, magic):
    rng = random.Random("t16-file-magic")
    blob = base64.b64encode(
        magic + bytes(rng.randrange(256) for _ in range(48))
    ).decode()
    spans = tier1.scan(f"attachment {blob} done", policy, registry)
    assert not [
        s for s in spans if s.cls == "secret" and s.status == "ambiguous"
    ]
    assert tier1.shannon_entropy(blob) > 4.0
