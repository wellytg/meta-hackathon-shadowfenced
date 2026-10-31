"""Redaction tests (T4): labels, numbering, re-hydration, TTL store.

Every secret is built at runtime from the canary registry, so no
secret literal appears in this file.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from shadowfenced import tier1
from shadowfenced.canary import CanaryRegistry
from shadowfenced.policy import load_policy
from shadowfenced.redact import (
    PLACEHOLDER_RE,
    STORE_TTL_S,
    _STORE,
    base_label,
    drop_mapping,
    load_mapping,
    redact,
    rehydrate,
    save_mapping,
)
from shadowfenced.spans import span_from_cp_range

SEED = "test-seed-t4"
KINDS = ("db_password", "api_key")


@pytest.fixture()
def policy():
    return load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")


@pytest.fixture()
def registry():
    return CanaryRegistry(SEED, KINDS)


def test_base_labels_per_class():
    assert base_label("secret", "canary") == "SECRET"
    assert base_label("secret", "provider_key") == "SECRET"
    assert base_label("pii", "email") == "PII_EMAIL"
    assert base_label("pii", "phone") == "PII_PHONE"
    assert base_label("business", "codename") == "CODENAME"
    assert base_label("business", "client") == "CLIENT"
    assert base_label("business", "figure") == "FIGURE"
    assert base_label("beacon", "beacon_cue") == "BEACON"


def test_placeholders_numbered_per_class(policy, registry):
    text = "mail ada@corvanelabs.com and bob@corvanelabs.com, call +1 (415) 555-0132"
    spans = tier1.scan(text, policy, registry)
    redacted, mapping = redact(text, spans)
    assert "[PII_EMAIL_1]" in redacted and "[PII_EMAIL_2]" in redacted
    assert "[PII_PHONE_1]" in redacted
    assert "ada@corvanelabs.com" not in redacted
    assert mapping["[PII_EMAIL_1]"] == "ada@corvanelabs.com"
    assert mapping["[PII_EMAIL_2]"] == "bob@corvanelabs.com"


def test_same_value_same_placeholder(policy, registry):
    text = "ada@corvanelabs.com wrote to ada@corvanelabs.com"
    spans = tier1.scan(text, policy, registry)
    assert len(spans) == 2
    redacted, mapping = redact(text, spans)
    assert redacted.count("[PII_EMAIL_1]") == 2
    assert len(mapping) == 1


def test_secret_codename_client_figure_beacon_placeholders(policy, registry):
    canary = registry.value("db_password")
    text = f"{canary} Project Tallow aster freight bookings $4.2M keys config.py"
    spans = tier1.scan(text, policy, registry)
    by_subclass = {s.subclass: s for s in spans}
    redacted, mapping = redact(
        text, [by_subclass[k] for k in by_subclass if k != "figure"]
    )
    assert "[SECRET_1]" in redacted and canary not in redacted
    assert "[CODENAME_1]" in redacted
    assert "[CLIENT_1]" in redacted
    figure = by_subclass.get("figure")
    if figure is not None:
        only_figure, figure_map = redact(text, [figure])
        assert "[FIGURE_1]" in only_figure
        assert list(figure_map) == ["[FIGURE_1]"]
    beacon_text = "removed the keys from config.py"
    beacon_spans = tier1.scan(beacon_text, policy, registry)
    assert beacon_spans and beacon_spans[0].cls == "beacon"
    beacon_redacted, _ = redact(beacon_text, beacon_spans)
    assert "[BEACON_1]" in beacon_redacted


def test_manual_spans_cover_all_listed_placeholders():
    cases = [
        ("secret", "entropy", "SECRET"),
        ("pii", "email", "PII_EMAIL"),
        ("pii", "phone", "PII_PHONE"),
        ("business", "codename", "CODENAME"),
        ("business", "client", "CLIENT"),
        ("business", "figure", "FIGURE"),
        ("beacon", "beacon_cue", "BEACON"),
    ]
    for cls, subclass, label in cases:
        text = f"value-{label}"
        span = span_from_cp_range(
            text, 0, len(text), cls=cls, subclass=subclass, status="proven",
            message="", why="", suggested_action="redact",
            can_keep=True, confidence=0.9,
        )
        redacted, mapping = redact(text, [span])
        assert redacted == f"[{label}_1]"
        assert mapping == {f"[{label}_1]": text}


def test_rehydrate_tolerant_variants():
    mapping = {"[SECRET_1]": "hunter2-value"}
    for variant in ("[SECRET_1]", "[secret_1]", "[secret-1]", "[SECRET 1]", "[ secret_1 ]"):
        text, highlights, unmatched = rehydrate(f"got {variant} ok", mapping)
        assert text == "got hunter2-value ok"
        assert unmatched == []
        assert highlights == [
            {"start": 4, "end": 4 + len("hunter2-value"), "placeholder": "[SECRET_1]"}
        ]


def test_rehydrate_bare_placeholder_names():
    mapping = {
        "[CODENAME_1]": "Bluefin Op",
        "[PII_EMAIL_1]": "ada@corvanelabs.com",
    }
    text, highlights, unmatched = rehydrate(
        "moved CODENAME_1 and PII_EMAIL_1 today", mapping
    )
    assert text == "moved Bluefin Op and ada@corvanelabs.com today"
    assert unmatched == []
    assert highlights == [
        {"start": 6, "end": 6 + len("Bluefin Op"),
         "placeholder": "[CODENAME_1]"},
        {"start": 6 + len("Bluefin Op") + 5,
         "end": 6 + len("Bluefin Op") + 5 + len("ada@corvanelabs.com"),
         "placeholder": "[PII_EMAIL_1]"},
    ]
    assert text[highlights[0]["start"]:highlights[0]["end"]] == "Bluefin Op"


def test_rehydrate_bare_names_need_word_boundaries():
    mapping = {"[CODENAME_1]": "Bluefin Op"}
    # Prefix/suffix word chars and longer numbers are not a match.
    text, highlights, unmatched = rehydrate(
        "XCODENAME_1 and CODENAME_1X and CODENAME_12 stay", mapping
    )
    assert text == "XCODENAME_1 and CODENAME_1X and CODENAME_12 stay"
    assert highlights == []
    assert unmatched == []
    # Unknown bare names are plain text, never unmatched.
    text, highlights, unmatched = rehydrate("saw FOO_1 here", mapping)
    assert (text, highlights, unmatched) == ("saw FOO_1 here", [], [])
    # Bare matching is case-insensitive, like the bracketed form.
    text, _, _ = rehydrate("moved codename_1 today", mapping)
    assert text == "moved Bluefin Op today"


def test_rehydrate_reports_unmatched_and_keeps_token():
    text, highlights, unmatched = rehydrate("saw [SECRET_9] here", {})
    assert text == "saw [SECRET_9] here"
    assert highlights == []
    assert unmatched == ["[SECRET_9]"]


def test_rehydrate_roundtrip_after_redact(policy, registry):
    canary = registry.value("db_password")
    text = f"deploy with {canary} and mail ada@corvanelabs.com"
    spans = [s for s in tier1.scan(text, policy, registry) if s.status == "proven"]
    redacted, mapping = redact(text, spans)
    answer = f"noted {redacted.split()[2]} and done"
    restored, highlights, unmatched = rehydrate(answer, mapping)
    assert canary in restored
    assert unmatched == []
    assert len(highlights) == 1
    start = highlights[0]["start"]
    end = highlights[0]["end"]
    assert restored[start:end] == canary


def test_placeholder_regex_matches_plan_shapes():
    for token in ("[SECRET_1]", "[PII_EMAIL_2]", "[CODENAME_1]", "[CLIENT_1]", "[BEACON_1]"):
        assert PLACEHOLDER_RE.search(token) is not None


def test_mapping_store_roundtrip_and_drop():
    save_mapping("dec-1", {"[SECRET_1]": "v"})
    assert load_mapping("dec-1") == {"[SECRET_1]": "v"}
    drop_mapping("dec-1")
    assert load_mapping("dec-1") == {}
    assert load_mapping("missing") == {}


def test_mapping_store_ttl_expiry(monkeypatch):
    assert STORE_TTL_S == 15 * 60
    save_mapping("dec-ttl", {"[SECRET_1]": "v"})
    assert load_mapping("dec-ttl") == {"[SECRET_1]": "v"}
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + STORE_TTL_S + 1)
    assert load_mapping("dec-ttl") == {}
    assert "dec-ttl" not in _STORE


def test_rehydrate_without_placeholders():
    text, highlights, unmatched = rehydrate("plain answer", {"[SECRET_1]": "v"})
    assert (text, highlights, unmatched) == ("plain answer", [], [])
