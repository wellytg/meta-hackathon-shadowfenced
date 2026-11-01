"""T48: re-identification must not warn about historical or public figures.

Ada Lovelace (1815) and Isaac Newton (1643) are named here because the
task requires them; every other person, place and value is invented.
Glimmer is never contacted: Tier 2 stays off in tests, so the local
Glimmer call returns None unless a test injects a stub judge.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import httpx

from shadowfenced import engine, reid
from shadowfenced.canary import CanaryRegistry
from shadowfenced.policy import load_policy

SEED = "test-seed-t48"
KINDS = ("db_password", "api_key")
POLICY_PATH = Path(__file__).resolve().parents[1] / "policy.yaml"

BROWSER_ADA = (
    "Summarise the life of Ada Lovelace, born 10 December 1815 in London."
)
HOLDOUT_ADA = (
    "I'm writing a short history blurb for our company newsletter. "
    "Can you give me two sentences about Ada Lovelace, "
    "born 10 December 1815, and why she's considered the first "
    "computer programmer?"
)
DANA = (
    "Find everything you can about Dana Whitlock, born 14 March 1991, "
    "who lives on Copperleaf Drive in Fairhaven."
)
MY_NAME = (
    "My name is Mara Vell, born 14 March 1989, "
    "I live at 9 Bellflower Lane."
)
LIVING_FRAMED = "Summarise the life of Wren Kado, born 3 April 1985."


def _policy():
    return load_policy(POLICY_PATH)


def _registry():
    return CanaryRegistry(SEED, KINDS)


def _public_figure_judge(text):
    return {
        "person_search": True,
        "subject_public": True,
        "subject": "Wren Kado",
        "inferred": ["Wren Kado"],
        "confidence": 0.9,
    }


def test_browser_ada_case_allows():
    result = reid.assess(BROWSER_ADA)
    assert result.outcome == "OK"
    assert "dob" not in result.combination
    decision = engine.gate(BROWSER_ADA, _policy(), _registry())
    assert decision.decision == "ALLOW"


def test_holdout_ada_blurb_allows():
    decision = engine.gate(HOLDOUT_ADA, _policy(), _registry())
    assert decision.decision == "ALLOW"


def test_isaac_newton_1643_allows():
    assert reid.assess("When was Isaac Newton born in 1643?").outcome == "OK"
    assert reid.assess(
        "Isaac Newton was born in 1643 and died in 1727.").outcome == "OK"


def test_made_up_1870_painter_allows():
    text = "Biography of Bramwell Quist, born 1870, painter in Dunmere."
    result = reid.assess(text)
    assert result.outcome == "OK"
    assert "dob" not in result.combination
    assert engine.gate(text, _policy(), _registry()).decision == "ALLOW"


def test_death_date_does_not_count():
    result = reid.assess("Ada Lovelace died on 27 November 1852 in London.")
    assert result.outcome == "OK"
    assert "dob" not in result.combination


def test_110_year_boundary():
    this_year = datetime.date.today().year
    living = reid.assess(f"Mara Vell, born 14 March {this_year - 110}.")
    assert living.outcome != "OK"
    assert "dob" in living.combination
    old = reid.assess(f"Mara Vell, born 14 March {this_year - 111}.")
    assert old.outcome == "OK"


def test_living_public_figure_mocked_allows():
    result = reid.assess(
        LIVING_FRAMED, public_figure_judge_fn=lambda text: True)
    assert result.outcome == "OK"


def test_living_inventor_question_mocked_allows():
    result = reid.assess(
        "What did Wren Kado invent in 1985?",
        public_figure_judge_fn=lambda text: {"public_figure": True})
    assert result.outcome == "OK"


def test_living_public_figure_judge_false_still_warns():
    result = reid.assess(
        LIVING_FRAMED, public_figure_judge_fn=lambda text: False)
    assert result.outcome == "HOLD"
    assert {"name", "dob"} <= set(result.combination)


def test_glimmer_failure_falls_back_to_date_rule():
    def _boom(text):
        raise TimeoutError("glimmer down")

    assert reid.assess(
        BROWSER_ADA, public_figure_judge_fn=_boom).outcome == "OK"
    assert reid.assess(
        LIVING_FRAMED, public_figure_judge_fn=_boom).outcome == "HOLD"


def test_public_figure_with_phone_still_warns():
    text = LIVING_FRAMED + " Call 555-0142."
    result = reid.assess(text, public_figure_judge_fn=lambda text: True)
    assert result.outcome == "HOLD"
    assert "phone" in result.combination


def test_public_figure_with_street_still_warns():
    text = "Biography of Wren Kado, born 3 April 1985, of 9 Bellflower Lane."
    result = reid.assess(text, public_figure_judge_fn=lambda text: True)
    assert result.outcome == "HOLD"
    assert "street" in result.combination


def test_dana_whitlock_still_warns():
    result = reid.assess(DANA)
    assert result.outcome == "HOLD"
    assert {"name", "dob"} <= set(result.combination)


def test_people_search_example_still_warns():
    def _private_judge(text):
        return {
            "person_search": True,
            "subject_public": False,
            "subject": "Dana Whitlock",
            "inferred": ["Dana Whitlock", "contact details"],
            "confidence": 0.9,
        }

    decision = engine.gate_reid(
        DANA, _policy(), _registry(), mode="send",
        person_search_judge_fn=_private_judge)
    assert decision.decision == "WARN"
    assert "person_search" in (decision.evidence or {})


def test_my_name_born_live_at_still_warns():
    assert reid.assess(MY_NAME).outcome == "HOLD"


def test_gate_living_public_figure_via_person_search_mock():
    decision = engine.gate(
        LIVING_FRAMED, _policy(), _registry(),
        person_search_judge_fn=_public_figure_judge)
    assert decision.decision == "ALLOW"


def test_no_network_calls(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(httpx, "Client", _forbidden)
    assert reid.assess(BROWSER_ADA).outcome == "OK"
    assert reid.assess(
        LIVING_FRAMED, public_figure_judge_fn=lambda text: True,
    ).outcome == "OK"
    assert engine.gate(BROWSER_ADA, _policy(), _registry()).decision == "ALLOW"


def test_public_figure_judge_unusable_falls_back():
    assert reid.assess(
        LIVING_FRAMED, public_figure_judge_fn=lambda text: "maybe",
    ).outcome == "HOLD"
    assert reid.assess(
        BROWSER_ADA, public_figure_judge_fn=lambda text: None,
    ).outcome == "OK"
