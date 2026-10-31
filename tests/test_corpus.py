"""Corpus tests (T14): 40 cases load, templates expand, file stays clean.

No test touches the network; every sensitive value is minted at runtime
from a seed. The file-shape scan rebuilds key prefixes from fragments so
this file can never match its own patterns.
"""

from __future__ import annotations

import re
from pathlib import Path

from shadowfenced import corpus
from shadowfenced.corpus import encoded_forms, expand_all, load_cases

SEED = "test-seed-corpus"

ROOT = Path(__file__).resolve().parents[1]


def _key_patterns() -> list[tuple[str, re.Pattern[str]]]:
    cloud_key = "AK" + "IA" + r"[0-9A-Z]{16}(?![0-9A-Z])"
    dev_token = "gh" + "p_" + r"[A-Za-z0-9]{36}(?![A-Za-z0-9])"
    live_secret = "s" + "k-" + r"[A-Za-z0-9]{20,}(?![A-Za-z0-9])"
    bot_token = "xox" + "b-" + r"[A-Za-z0-9-]{10,}"
    return [
        ("cloud_key", re.compile(cloud_key)),
        ("dev_token", re.compile(dev_token)),
        ("live_secret", re.compile(live_secret)),
        ("bot_token", re.compile(bot_token)),
    ]


def test_loads_40_cases_with_expected_mix():
    cases = load_cases()
    assert len(cases) == 40
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["category"]] = counts.get(case["category"], 0) + 1
    assert counts == {
        "secrets": 10,
        "pii": 7,
        "business": 4,
        "beacon": 3,
        "benign": 16,
    }


def test_benign_share_at_least_40_percent():
    cases = load_cases()
    benign = sum(1 for case in cases if case["category"] == "benign")
    assert benign / len(cases) >= 0.40


def test_templates_expand_at_runtime():
    cases = expand_all(load_cases(), SEED)
    assert len(cases) == 40
    for case in cases:
        assert "{{" not in case["text"], case["id"]
        assert "}}" not in case["text"], case["id"]
        if case["category"] == "benign":
            assert case["sensitive"] == [], case["id"]
        else:
            assert case["sensitive"], case["id"]
        for value in case["sensitive"]:
            assert "{{" not in value, case["id"]
            forms = encoded_forms(value) + [" ".join(value)]
            assert any(form and form in case["text"] for form in forms), (
                case["id"],
                value[:24],
            )


def test_expansion_is_deterministic():
    raw = load_cases()
    first = expand_all(raw, SEED)
    second = expand_all(raw, SEED)
    assert [c["text"] for c in first] == [c["text"] for c in second]
    assert [c["sensitive"] for c in first] == [c["sensitive"] for c in second]


def test_corpus_file_has_no_key_shapes():
    paths = [ROOT / "corpus" / "cases.yaml",
             ROOT / "src" / "shadowfenced" / "corpus.py"]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "SF_CANARY_" not in text, path
        for kind, pattern in _key_patterns():
            assert not pattern.search(text), f"{path} looks like {kind}"


def test_file_case_values_match_generated_files():
    cases = {c["id"]: c for c in expand_all(load_cases(), SEED)}
    customers = corpus.file_text("customers.csv")
    assert "ada.lark@corvane.io" in customers
    assert "+1 (415) 555-0132" in customers
    for value in cases["f-customers"]["sensitive"]:
        assert value in customers, value
    roadmap = corpus.file_text("roadmap.docx")
    for value in cases["f-roadmap"]["sensitive"]:
        assert value in roadmap, value
    assert cases["f-notes"]["text"].strip()
