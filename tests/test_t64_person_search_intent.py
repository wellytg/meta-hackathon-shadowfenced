"""T64: a reply to a contact is not a person search.

"Draft a reply to Mara Voss, ..." names a contact with identifying
details but asks for no lookup, so the person-search rule must not
fire: the re-identification outcome applies instead. Real lookups
("Find everything about Daniel Whitmore ...", "Look up Theo
Lindqvist at 8 Harbor Lane") still warn. Every person, place and
value below is invented for the test.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from shadowfenced import hosted as hosted_module
from shadowfenced import person_search as ps_module
from shadowfenced.canary import CanaryRegistry
from shadowfenced.engine import gate
from shadowfenced.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
SEED = "test-seed-t64"
KINDS = ("db_password", "api_key")

MARA = (
    "Draft a reply to Mara Voss, her number is 415-555-0132 "
    "and her email is mara.voss@brightpost.net"
)
DANIEL = (
    "Find everything about Daniel Whitmore, born 14 March 1987, "
    "lives in Leeds"
)
THEO_LOOKUP = "Look up Theo Lindqvist at 8 Harbor Lane"
THEO_HIRE = "Is Theo Lindqvist a good hire? He lives at 8 Harbor Lane."

RAW_VALUES = (
    "Mara Voss", "415-555-0132", "mara.voss@brightpost.net",
    "Daniel Whitmore", "14 March 1987",
    "Theo Lindqvist", "8 Harbor Lane",
)


def _policy():
    return load_policy(ROOT / "policy.yaml")


def _registry():
    return CanaryRegistry(SEED, KINDS)


def _lookup_judge(name, clues):
    def judge(text):
        return {
            "person_search": True,
            "subject_public": False,
            "subject": name,
            "inferred": list(clues),
            "confidence": 0.9,
            "intent": "lookup",
        }

    return judge


def _no_values_leak(decision, trace):
    blob = " ".join(decision.reasons) + json.dumps(trace)
    for raw in RAW_VALUES:
        assert raw not in blob, raw


def test_reply_to_contact_is_not_person_search():
    trace: list = []
    decision = gate(
        MARA, _policy(), _registry(), mode="send", trace_out=trace,
        person_search_judge_fn=_lookup_judge(
            "Mara Voss", ["Mara Voss", "415-555-0132"]),
    )
    assert "person_search" not in (decision.evidence or {})
    assert "person-search" not in decision.rule_ids
    assert "reidentification" in decision.rule_ids
    assert decision.decision == "REDACT"
    _no_values_leak(decision, trace)


def test_reply_with_other_intent_is_not_person_search():
    def judge(text):
        return {
            "person_search": False,
            "subject_public": False,
            "subject": "Mara Voss",
            "inferred": ["Mara Voss"],
            "confidence": 0.9,
            "intent": "other",
        }

    trace: list = []
    decision = gate(
        MARA, _policy(), _registry(), mode="send", trace_out=trace,
        person_search_judge_fn=judge,
    )
    assert "person_search" not in (decision.evidence or {})
    assert "person-search" not in decision.rule_ids
    assert decision.decision == "REDACT"
    _no_values_leak(decision, trace)


def test_real_search_still_warns():
    trace: list = []
    decision = gate(
        DANIEL, _policy(), _registry(), mode="send", trace_out=trace,
        person_search_judge_fn=_lookup_judge(
            "Daniel Whitmore", ["Daniel Whitmore", "14 March 1987"]),
    )
    assert decision.decision == "WARN"
    person = (decision.evidence or {}).get("person_search")
    assert person is not None
    assert person["banner"].startswith("This message names a person")
    assert "Rule: Person search." in person["banner"]
    assert "Daniel Whitmore" not in person["banner"]
    assert "14 March 1987" not in person["banner"]
    assert "person-search" in decision.rule_ids
    assert "reidentification" in decision.rule_ids
    _no_values_leak(decision, trace)


def test_lookup_still_warns():
    trace: list = []
    decision = gate(
        THEO_LOOKUP, _policy(), _registry(), mode="send", trace_out=trace,
        person_search_judge_fn=_lookup_judge(
            "Theo Lindqvist", ["Theo Lindqvist", "8 Harbor Lane"]),
    )
    person = (decision.evidence or {}).get("person_search")
    assert person is not None
    assert "Rule: Person search." in person["banner"]
    assert "Theo Lindqvist" not in person["banner"]
    assert "8 Harbor Lane" not in person["banner"]
    assert "person-search" in decision.rule_ids
    assert decision.decision != "ALLOW"
    _no_values_leak(decision, trace)


def test_hire_question_is_not_person_search():
    trace: list = []
    decision = gate(
        THEO_HIRE, _policy(), _registry(), mode="send", trace_out=trace,
        person_search_judge_fn=_lookup_judge(
            "Theo Lindqvist", ["Theo Lindqvist", "8 Harbor Lane"]),
    )
    assert "person_search" not in (decision.evidence or {})
    assert "person-search" not in decision.rule_ids
    _no_values_leak(decision, trace)


def test_prompt_asks_for_lookup_intent():
    system = ps_module.PERSON_SEARCH_SYSTEM
    assert "intent is lookup" in system
    assert "other for replies" in system
    assert "FIND" in system and "LOOK UP" in system


def _glimmer_transport(verdict: dict) -> httpx.MockTransport:
    body = json.dumps(verdict)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/chat")
        return httpx.Response(
            200, json={"message": {"content": body}})

    return httpx.MockTransport(handler)


def test_local_glimmer_reply_is_not_search(monkeypatch):
    monkeypatch.delenv("SF_HOSTED", raising=False)
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    hostile = {
        "person_search": True,
        "subject_public": False,
        "subject": "Mara Voss",
        "inferred": ["Mara Voss", "415-555-0132"],
        "confidence": 0.9,
        "intent": "lookup",
    }
    result = ps_module.check_person_search(
        MARA, transport=_glimmer_transport(hostile))
    assert result.warn is False
    assert result.banner == ""


def test_local_glimmer_search_warns(monkeypatch):
    monkeypatch.delenv("SF_HOSTED", raising=False)
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    verdict = {
        "person_search": True,
        "subject_public": False,
        "subject": "Daniel Whitmore",
        "inferred": ["Daniel Whitmore", "14 March 1987"],
        "confidence": 0.9,
        "intent": "lookup",
    }
    result = ps_module.check_person_search(
        DANIEL, transport=_glimmer_transport(verdict))
    assert result.warn is True
    assert "Rule: Person search." in result.banner
    assert "Daniel Whitmore" not in result.banner


def _meta_transport(verdict: dict) -> httpx.MockTransport:
    body = json.dumps(verdict)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200, json={"choices": [{"message": {"content": body}}]})
        return httpx.Response(500, json={})

    return httpx.MockTransport(handler)


def test_hosted_spark_reply_is_not_search(monkeypatch):
    monkeypatch.setenv("SF_HOSTED", "1")
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("META_API_KEY", "test-meta-key")
    monkeypatch.setattr(hosted_module, "ollama_reachable",
                        lambda **kw: False)
    hostile = {
        "person_search": True,
        "subject_public": False,
        "subject": "Mara Voss",
        "inferred": ["Mara Voss", "415-555-0132"],
        "confidence": 0.9,
        "intent": "lookup",
    }
    result = ps_module.check_person_search(
        MARA, transport=_meta_transport(hostile))
    assert result.warn is False
    assert result.banner == ""


def test_hosted_spark_search_warns(monkeypatch):
    monkeypatch.setenv("SF_HOSTED", "1")
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("META_API_KEY", "test-meta-key")
    monkeypatch.setattr(hosted_module, "ollama_reachable",
                        lambda **kw: False)
    verdict = {
        "person_search": True,
        "subject_public": False,
        "subject": "Daniel Whitmore",
        "inferred": ["Daniel Whitmore", "14 March 1987"],
        "confidence": 0.9,
        "intent": "lookup",
    }
    result = ps_module.check_person_search(
        DANIEL, transport=_meta_transport(verdict))
    assert result.warn is True
    assert "Rule: Person search." in result.banner
    assert "Daniel Whitmore" not in result.banner
    assert "14 March 1987" not in result.banner
