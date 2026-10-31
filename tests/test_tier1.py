"""Tier 1 tests (T2 secrets + T3 policy/PII/business/beacon).

Every secret is built at runtime from the canary registry or from key-shape
fragments joined here, so no real-format literal appears in this file.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from shadowfenced import tier1
from shadowfenced.canary import (
    CanaryRegistry,
    decode_encoding,
    encodings,
    make_canary,
)
from shadowfenced.policy import load_policy
from shadowfenced.spans import (
    cp_to_utf16,
    make_span_id,
    merge_overlaps,
    span_from_cp_range,
    utf16_to_cp,
)

SEED = "test-seed-t2t3"
KINDS = ("db_password", "api_key")

# Provider key shapes, assembled at runtime (never one literal).
CLOUD_KEY = "AK" + "IA" + "ABCDEFGH12345678"
DEV_TOKEN = "gh" + "p_" + "a" * 36
LIVE_SECRET = "s" + "k-" + "b" * 24
BOT_TOKEN = "xox" + "b-" + "c" * 12

ENTROPY_TOKEN = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"


@pytest.fixture()
def policy():
    return load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")


@pytest.fixture()
def registry():
    return CanaryRegistry(SEED, KINDS)


def _canary(registry):
    return registry.value("db_password")


# --- canary + policy + span units -------------------------------------------


def test_make_canary_format_is_deterministic():
    first = make_canary(SEED, "db_password")
    assert first == make_canary(SEED, "db_password")
    assert first.startswith("SF_CANARY_DB_PASSWORD_")
    suffix = first.rsplit("_", 1)[1]
    assert len(suffix) == 16 and all(c in "0123456789abcdef" for c in suffix)
    assert make_canary(SEED, "api_key") != first


def test_encodings_roundtrip():
    value = make_canary(SEED, "db_password")
    for name, blob in encodings(value).items():
        if name in ("exact", "url"):
            assert value in blob or blob == value
        else:
            assert decode_encoding(blob, name) == value


def test_policy_loads_corvane_labs(policy):
    assert policy.org == "Corvane Labs"
    assert policy.action_for("secret") == "block"
    assert policy.keepable("secret") is False
    assert policy.keepable("pii") is True
    assert {c.name for c in policy.codenames} >= {"TALLOW", "BLUEFIN", "HERON"}


def test_span_id_is_short_hash():
    assert make_span_id("secret", 3, 10) == make_span_id("secret", 3, 10)
    assert len(make_span_id("secret", 3, 10)) == 12
    assert make_span_id("secret", 3, 10) != make_span_id("pii", 3, 10)


# --- T2: canaries ------------------------------------------------------------


def test_canary_exact_proven(policy, registry):
    value = _canary(registry)
    spans = tier1.scan(f"deploy with {value} tonight", policy, registry)
    hit = [s for s in spans if s.cls == "secret" and s.status == "proven"]
    assert hit and hit[0].suggested_action == "block"
    assert hit[0].can_keep is False


@pytest.mark.parametrize("form", ["base64", "hex", "rot13", "reversed", "url"])
def test_canary_encoded_forms_proven(policy, registry, form):
    blob = encodings(_canary(registry))[form]
    spans = tier1.scan(f"debug log: {blob}", policy, registry)
    assert any(
        s.cls == "secret" and s.status == "proven"
        and s.suggested_action == "block"
        for s in spans
    ), f"no proven secret span for {form} form"


def test_canary_split_across_whitespace_and_newlines_proven(policy, registry):
    value = _canary(registry)
    spaced = " ".join(value)
    broken = "\n".join(value)
    spans = tier1.scan(f"a {spaced} b", policy, registry)
    assert any(s.cls == "secret" and s.status == "proven" for s in spans)
    spans = tier1.scan(f"a {broken} b", policy, registry)
    assert any(s.cls == "secret" and s.status == "proven" for s in spans)


def test_any_sf_canary_token_proven(policy, registry):
    token = "SF_" + "CANARY_" + "UNKNOWN_" + "0123456789abcdef"
    spans = tier1.scan(f"leaked {token} here", policy, registry)
    assert any(
        s.cls == "secret" and s.status == "proven" for s in spans
    )


# --- T2: provider keys + password assignment + entropy ----------------------


@pytest.mark.parametrize(
    "key", [CLOUD_KEY, DEV_TOKEN, LIVE_SECRET, BOT_TOKEN],
)
def test_provider_key_shapes_proven(policy, registry, key):
    spans = tier1.scan(f"config holds {key} do not share", policy, registry)
    assert any(
        s.cls == "secret" and s.status == "proven"
        and s.subclass == "provider_key"
        and s.suggested_action == "block"
        for s in spans
    ), f"no proven provider_key span for shape ending ...{key[-6:]}"


@pytest.mark.parametrize(
    "label", ["password", "passwd", "secret", "token"],
)
def test_password_assignment_proven(policy, registry, label):
    spans = tier1.scan(f"login {label}: hunter2-value", policy, registry)
    assert any(
        s.cls == "secret" and s.status == "proven"
        and s.subclass == "password_assignment"
        for s in spans
    )


def test_entropy_token_ambiguous(policy, registry):
    spans = tier1.scan(f"here is {ENTROPY_TOKEN} maybe", policy, registry)
    assert any(
        s.cls == "secret" and s.status == "ambiguous"
        and s.subclass == "entropy"
        and s.suggested_action == "review"
        for s in spans
    )


def test_entropy_allowlist_git_sha_and_uuid_ignored(policy, registry):
    sha = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    spans = tier1.scan(f"commit {sha} run {uuid} done", policy, registry)
    assert spans == []


def test_tier1_under_20ms_on_4kb(policy, registry):
    value = _canary(registry)
    filler = "The quick brown fox jumps over the lazy dog. " * 90
    text = (filler + f" contact {value} ").ljust(4096)[:4096]
    assert len(text) == 4096
    tier1.scan(text, policy, registry)  # warm up compiled patterns
    started = time.perf_counter()
    spans = tier1.scan(text, policy, registry)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert any(s.cls == "secret" for s in spans)
    assert elapsed_ms < 20, f"Tier 1 took {elapsed_ms:.1f} ms on 4 KB"


# --- T3: PII -----------------------------------------------------------------


def test_email_proven_and_allowlist_ignored(policy, registry):
    spans = tier1.scan("mail ada@corvanelabs.com now", policy, registry)
    hit = [s for s in spans if s.subclass == "email"]
    assert len(hit) == 1 and hit[0].status == "proven"
    assert hit[0].suggested_action == "redact" and hit[0].can_keep is True
    for domain in ("example.com", "example.org", "example.net"):
        clean = tier1.scan(f"mail ada@{domain} now", policy, registry)
        assert clean == [], f"allowlisted {domain} was flagged"


def test_phone_proven(policy, registry):
    spans = tier1.scan("call +1 (415) 555-0132 today", policy, registry)
    assert any(s.subclass == "phone" and s.status == "proven" for s in spans)


def test_ssn_shape_proven(policy, registry):
    spans = tier1.scan("ssn 078-05-1120 on file", policy, registry)
    assert any(s.subclass == "ssn" and s.status == "proven" for s in spans)


def test_employee_id_near_name_proven(policy, registry):
    spans = tier1.scan("Priya Nair (EMP-482913) onboarded", policy, registry)
    assert any(
        s.subclass == "employee_id" and s.status == "proven" for s in spans
    )


def test_employee_id_without_name_ignored(policy, registry):
    assert tier1.scan("lookup EMP-482913 in hr", policy, registry) == []


def test_luhn_card_proven_and_invalid_ignored(policy, registry):
    spans = tier1.scan("card 4111 1111 1111 1111 charged", policy, registry)
    assert any(s.subclass == "card" and s.status == "proven" for s in spans)
    assert tier1.scan("card 4111 1111 1111 1112 typo", policy, registry) == []


# --- T3: business ------------------------------------------------------------


def test_codename_needs_project_or_caps(policy, registry):
    flagged = tier1.scan("Project Tallow slips to Q3", policy, registry)
    assert any(s.subclass == "codename" and s.status == "proven" for s in flagged)
    assert tier1.scan("tallow candles smell nice", policy, registry) == []
    assert tier1.scan("we saw a heron by the lake", policy, registry) == []


def test_codename_allcaps_flagged(policy, registry):
    spans = tier1.scan("TALLOW ships Friday", policy, registry)
    assert any(s.subclass == "codename" for s in spans)


def test_codename_hyphen_insensitive(policy, registry):
    spans = tier1.scan("Project Blue-Fin slips to Q3", policy, registry)
    assert any(s.subclass == "codename" for s in spans)


def test_client_name_proven(policy, registry):
    spans = tier1.scan("aster freight renewed today", policy, registry)
    assert any(
        s.subclass == "client" and s.status == "proven"
        and s.suggested_action == "redact"
        for s in spans
    )


def test_figure_cue_with_number_proven(policy, registry):
    spans = tier1.scan("bookings hit $4.2M before the call", policy, registry)
    assert any(s.subclass == "figure" and s.status == "proven" for s in spans)
    assert tier1.scan("no hotel bookings this week", policy, registry) == []


# --- T3: beacon --------------------------------------------------------------


def test_beacon_cue_ambiguous(policy, registry):
    spans = tier1.scan("removed the keys from config.py", policy, registry)
    assert any(
        s.cls == "beacon" and s.status == "ambiguous"
        and s.suggested_action == "review"
        for s in spans
    )


def test_beacon_needs_noun_and_location(policy, registry):
    assert tier1.scan("the keys are strong", policy, registry) == []
    assert tier1.scan("config.py was updated", policy, registry) == []


# --- offsets, merging, benign -------------------------------------------------


def test_utf16_offsets_with_emoji(policy, registry):
    value = _canary(registry)
    text = f"\U0001F389 {value}"
    spans = tier1.scan(text, policy, registry)
    hit = next(s for s in spans if s.cls == "secret" and s.status == "proven")
    assert hit.cp_start == 2 and text[hit.cp_start:hit.cp_end] == value
    assert hit.start == 3 and hit.end == 3 + len(value)
    assert cp_to_utf16(text, hit.cp_start) == hit.start
    assert utf16_to_cp(text, hit.start) == hit.cp_start


def test_merge_overlap_keeps_proven(policy, registry):
    spans = tier1.scan(f"token: {ENTROPY_TOKEN}", policy, registry)
    assert len(spans) == 1
    assert spans[0].status == "proven"
    assert spans[0].suggested_action == "block"


def test_merge_overlaps_helper_prefers_severe():
    text = "x" * 40
    weak = span_from_cp_range(
        text, 5, 15, cls="business", subclass="client", status="proven",
        message="", why="", suggested_action="redact",
        can_keep=True, confidence=0.9,
    )
    strong = span_from_cp_range(
        text, 8, 20, cls="secret", subclass="entropy", status="ambiguous",
        message="", why="", suggested_action="review",
        can_keep=False, confidence=0.55,
    )
    merged = merge_overlaps([weak, strong])
    assert len(merged) == 1
    assert (merged[0].cp_start, merged[0].cp_end) == (5, 20)
    assert merged[0].cls == "secret"


def test_benign_prose_clean(policy, registry):
    text = (
        "We saw a heron by the lake near the tallow candles stall. "
        "Email ada@example.com for hotel bookings."
    )
    assert tier1.scan(text, policy, registry) == []
