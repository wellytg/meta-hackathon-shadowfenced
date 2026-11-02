"""T33 person-search intent: Glimmer reads the whole statement.

Every person, place and value below is invented for the test.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from shadowfenced import person_search as ps_module
from shadowfenced.canary import CanaryRegistry
from shadowfenced.engine import gate_reid, user_message
from shadowfenced.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
SEED = "test-seed-person-search"
KINDS = ("db_password", "api_key")


def _policy():
    return load_policy(ROOT / "policy.yaml")


def _registry():
    return CanaryRegistry(SEED, KINDS)


PRIVATE_TEXT = "Find Dana Whitlock for me, I need her contact details."


def _private_judge(text):
    return {
        "person_search": True,
        "subject_public": False,
        "subject": "Dana Whitlock",
        "inferred": ["Dana Whitlock", "contact details"],
        "confidence": 0.9,
    }


def test_private_search_warns_with_banner_and_generalise():
    trace = []
    decision = gate_reid(
        PRIVATE_TEXT, _policy(), _registry(),
        person_search_judge_fn=_private_judge, trace_out=trace,
    )
    assert decision.decision == "WARN"
    assert decision.sent_text == PRIVATE_TEXT
    info = (decision.evidence or {}).get("person_search")
    assert info is not None
    assert info["banner"].startswith("This message names a person")
    assert "identify them" in info["banner"]
    assert "If this is you" in info["banner"]
    assert "Rule: Person search" in info["banner"]
    assert info["generalised_text"]
    assert "Dana Whitlock" not in info["generalised_text"]
    assert user_message(decision) == info["banner"]
    steps = [s["step"] for s in trace]
    assert "intent.person_search" in steps
    intent = next(s for s in trace if s["step"] == "intent.person_search")
    assert intent["result"] == "WARN"
    for raw in ("Dana Whitlock", "contact details"):
        assert raw not in intent["detail"]
    assert any("reid-generalise" in txt or "Generalise" in txt for txt in [
        (ROOT / "src" / "shadowfenced" / "static" / "reid.js").read_text(encoding="utf-8"),
        (ROOT / "src" / "shadowfenced" / "static" / "app.js").read_text(encoding="utf-8"),
    ])


def test_historical_figure_is_not_flagged():
    def _judge(text):
        return {
            "person_search": True,
            "subject_public": True,
            "subject": "Edmund Harrow",
            "inferred": ["Edmund Harrow"],
            "confidence": 0.9,
        }

    text = "When was Edmund Harrow born and where did he live?"
    decision = gate_reid(
        text, _policy(), _registry(), person_search_judge_fn=_judge,
    )
    assert decision.decision != "WARN"
    assert decision.decision != "BLOCK"
    assert decision.decision != "HOLD"


def test_public_figure_in_public_role_is_not_flagged():
    def _judge(text):
        return {
            "person_search": True,
            "subject_public": True,
            "subject": "Elena Vasquez",
            "inferred": ["Elena Vasquez"],
            "confidence": 0.95,
        }

    text = "Mayor Elena Vasquez of Port Aubrun opened the new library on Saturday."
    decision = gate_reid(
        text, _policy(), _registry(), person_search_judge_fn=_judge,
    )
    assert decision.decision == "ALLOW"


def test_unverifiable_claims_are_dropped():
    def _judge(text):
        return {
            "person_search": True,
            "subject_public": False,
            "subject": "Nobody Here",
            "inferred": ["not in the message at all"],
            "confidence": 0.99,
        }

    decision = gate_reid(
        PRIVATE_TEXT, _policy(), _registry(), person_search_judge_fn=_judge,
    )
    assert decision.decision != "WARN" or "person_search" not in (decision.evidence or {})


def test_low_confidence_does_not_warn():
    def _judge(text):
        return {
            "person_search": True,
            "subject_public": False,
            "subject": "Dana Whitlock",
            "inferred": ["Dana Whitlock"],
            "confidence": 0.5,
        }

    decision = gate_reid(
        PRIVATE_TEXT, _policy(), _registry(), person_search_judge_fn=_judge,
    )
    assert "person_search" not in (decision.evidence or {})
    assert decision.decision != "BLOCK"


def test_glimmer_failure_falls_back_and_never_blocks():
    trace = []

    def _judge(text):
        return None

    decision = gate_reid(
        PRIVATE_TEXT, _policy(), _registry(),
        person_search_judge_fn=_judge, trace_out=trace,
    )
    assert decision.decision != "BLOCK"
    assert "person_search" not in (decision.evidence or {})
    intent = next(s for s in trace if s["step"] == "intent.person_search")
    assert "Glimmer could not check intent" in intent["detail"]

    def _boom(text):
        raise TimeoutError("glimmer down")

    trace2 = []
    decision2 = gate_reid(
        "hello world", _policy(), _registry(),
        person_search_judge_fn=_boom, trace_out=trace2,
    )
    assert decision2.decision == "ALLOW"


def test_no_trigger_skips_glimmer():
    calls = []

    def _judge(text):
        calls.append(text)
        return _private_judge(text)

    decision = gate_reid(
        "The office printer is on the third floor.", _policy(), _registry(),
        person_search_judge_fn=_judge,
    )
    assert calls == []
    assert "person_search" not in (decision.evidence or {})


def test_no_raw_values_in_trace_or_evidence():
    trace = []
    decision = gate_reid(
        PRIVATE_TEXT, _policy(), _registry(),
        person_search_judge_fn=_private_judge, trace_out=trace,
    )
    info = (decision.evidence or {})["person_search"]
    blob = " ".join([
        info["banner"],
        " ".join(info["inferred"]),
        " ".join(info["inferred_fingerprints"]),
        info["subject_fingerprint"],
        info["generalised_text"],
    ])
    for raw in ("Dana Whitlock",):
        assert raw not in blob
        for step in trace:
            if step["step"] == "intent.person_search":
                assert raw not in step["detail"]
    for reason in decision.reasons:
        assert "Dana Whitlock" not in reason


def test_conversation_search_warns():
    prior = ["Do you know Goran Vex?"]

    def _judge(text):
        assert "Goran Vex" in text
        return {
            "person_search": True,
            "subject_public": False,
            "subject": "Goran Vex",
            "inferred": ["Goran Vex", "contact details"],
            "confidence": 0.85,
        }

    decision = gate_reid(
        "Find his contact details for me.", _policy(), _registry(),
        conversation=prior, person_search_judge_fn=_judge,
    )
    assert decision.decision == "WARN"


def test_corpus_has_twenty_fictional_cases():
    raw = yaml.safe_load(
        (ROOT / "corpus" / "person_search.yaml").read_text(encoding="utf-8"))
    assert len(raw) == 20
    risky = [c for c in raw if c["category"] == "risky"]
    safe = [c for c in raw if c["category"] == "safe"]
    assert len(risky) == 12
    assert len(safe) == 8
    for case in raw:
        assert case["expected"] in ("WARN", "OK")
