"""Re-identification tests (T30): extraction, scoring, generalisation, residual.

Every person, place and value below is invented for the test.
"""

from __future__ import annotations

from pathlib import Path

from shadowfenced import engine, reid, tier1
from shadowfenced.canary import CanaryRegistry
from shadowfenced.policy import load_policy

SEED = "test-seed-reid"
KINDS = ("db_password", "api_key")
POLICY_PATH = Path(__file__).resolve().parents[1] / "policy.yaml"

DOB_TEXT = "Mara Vell, born 14 March 1989, of 9 Bellflower Lane, Dunmere."


def _policy():
    return load_policy(POLICY_PATH)


def _registry():
    return CanaryRegistry(SEED, KINDS)


def _kinds(text):
    return {item["kind"] for item in reid.extract_quasi_identifiers(text)}


def test_extract_name_dob_street_city():
    findings = reid.extract_quasi_identifiers(DOB_TEXT)
    by_kind = {}
    for item in findings:
        by_kind.setdefault(item["kind"], []).append(item["value"])
    assert by_kind["name"] == ["Mara Vell"]
    assert by_kind["dob"] == ["14 March 1989"]
    assert by_kind["street"] == ["9 Bellflower Lane"]
    assert "Dunmere" in by_kind["city"]
    for item in findings:
        start, end = item["span"]
        assert DOB_TEXT[start:end] == item["value"]


def test_extract_redaction_token_as_name():
    findings = reid.extract_quasi_identifiers("Patient [PII_NAME_1] was seen.")
    assert [item["kind"] for item in findings] == ["name"]


def test_extract_age_forms():
    assert "age" in _kinds("Tilda Renn, 7 years old, was admitted.")
    assert "age" in _kinds("Danko Miri, 61, retired last year.")
    assert "age" in _kinds("He just turned 60 last week.")
    assert "age" in _kinds("Bram Odo of Dunmere, 40s, likes cycling.")


def test_extract_employer_jobtitle_school():
    kinds = _kinds("The only paediatric surgeon at Fairhaven clinic, "
                   "trained at Bramble School.")
    assert {"employer", "jobtitle", "school"} <= kinds


def test_extract_gender_medical_plate_dated():
    text = ("She takes dialysis; plate ending 441; "
            "admitted 3 May; phone 555-0103.")
    kinds = _kinds(text)
    assert {"gender", "medical", "plate", "dated_event", "phone"} <= kinds


def test_extract_postcode_and_email():
    kinds = _kinds("Write to joren.pike@corvane.io in 90274.")
    assert {"email", "postcode"} <= kinds


def test_single_detail_is_ok():
    assert reid.assess("Call 555-0100 for the main switchboard.").outcome == "OK"
    assert reid.assess("Rain is expected in Dunmere tomorrow.").outcome == "OK"


def test_high_risk_pairs_hold():
    assert reid.assess("Mara Vell, born 14 March 1989.").outcome == "HOLD"
    assert reid.assess("Mara Vell of 9 Bellflower Lane.").outcome == "HOLD"
    assert reid.assess(
        "Born 14 March 1989, postcode 90274, female.").outcome == "HOLD"
    assert reid.assess(
        "Our nurse at Fairhaven clinic in Dunmere.").outcome == "HOLD"


def test_banner_lists_kinds_not_values():
    result = reid.assess(DOB_TEXT)
    assert result.outcome == "HOLD"
    assert result.banner.startswith("Together, your ")
    assert "name" in result.banner and "date of birth" in result.banner
    assert "Mara Vell" not in result.banner
    assert "1989" not in result.banner


def test_conversation_accumulates():
    acc = reid.ConversationAccumulator()
    assert acc.score()[2] == "OK"
    acc.add_message("My neighbour works down at the harbour.")
    assert acc.score()[2] == "OK"
    acc.add_message("He is the only pilot-boat captain there.")
    acc.add_message("He just turned 60 last week.")
    score, combination, outcome = acc.score()
    assert outcome == "HOLD"
    assert {"jobtitle", "age"} <= set(combination)
    acc.reset()
    assert acc.score()[2] == "OK"
    via_engine = reid.assess(
        "He just turned 60 last week.",
        conversation=["My neighbour works down at the harbour.",
                      "He is the only pilot-boat captain there."])
    assert via_engine.outcome == "HOLD"


def test_exemptions_public_figure_and_story():
    mayor = reid.assess(
        "Mayor Elena Vasquez of Port Aubrun opened the new library.")
    assert mayor.outcome == "OK" and mayor.exempted
    story = reid.assess("In my novel, Captain Ilsa Renn sails away.")
    assert story.outcome == "OK" and story.exempted


def test_generalise_rules():
    findings = reid.extract_quasi_identifiers(DOB_TEXT)
    new_text, mapping = reid.generalise(DOB_TEXT, findings)
    assert "14 March 1989" not in new_text
    assert "1989" in new_text  # dob -> year
    assert "9 Bellflower Lane" not in new_text
    assert "Dunmere" in new_text  # street -> city
    assert "Mara Vell" not in new_text  # name -> initials
    assert "M.V." in new_text
    assert mapping and all(raw in DOB_TEXT for raw in mapping.values())

    postcode_text = "Postcode 90274."
    postcode_new, _ = reid.generalise(
        postcode_text, reid.extract_quasi_identifiers(postcode_text))
    assert "902**" in postcode_new

    title_text = "Our night-shift pharmacist retired."
    title_new, _ = reid.generalise(
        title_text, reid.extract_quasi_identifiers(title_text))
    assert "pharmacy staff" in title_new

    age_text = "Tilda Renn, 7 years old."
    age_new, _ = reid.generalise(
        age_text, reid.extract_quasi_identifiers(age_text))
    assert "7 years old" not in age_new


def _fake_judge(answer):
    def _judge(_text):
        return dict(answer)
    return _judge


def test_residual_hold_with_verified_clues():
    redacted = ("[NAME], born 1988, the only paediatric surgeon at "
                "Fairhaven clinic, is on call tonight.")
    judge = _fake_judge({
        "can_identify": True, "can_locate": False,
        "clues": ["only paediatric surgeon", "Fairhaven clinic", "nope"],
    })
    result = reid.residual_check(redacted, judge)
    assert result.hold is True
    assert result.verified_clues == [
        "only paediatric surgeon", "Fairhaven clinic"]


def test_residual_ok_without_two_verified_clues():
    redacted = ("[NAME], born 1988, the only paediatric surgeon at "
                "Fairhaven clinic, is on call tonight.")
    judge = _fake_judge({
        "can_identify": True, "can_locate": False,
        "clues": ["only paediatric surgeon", "invented phrase"],
    })
    result = reid.residual_check(redacted, judge)
    assert result.hold is False


def test_residual_fail_closed_on_judge_error():
    redacted = ("[NAME], born 1988, the only paediatric surgeon at "
                "Fairhaven clinic, is on call tonight.")

    def _boom(_text):
        raise TimeoutError("judge timed out")

    assert reid.residual_check(redacted, _boom).hold is True
    assert reid.residual_check(redacted, lambda _t: None).hold is True
    assert reid.residual_check(redacted, lambda _t: "maybe").hold is True
    benign = reid.residual_check(
        redacted, lambda _t: {"can_identify": False, "can_locate": False,
                              "clues": []})
    assert benign.hold is False


def test_residual_skips_judge_when_little_remains():
    calls: list[str] = []

    def _spy(text):
        calls.append(text)
        return {"can_identify": True, "can_locate": True, "clues": [text]}

    result = reid.residual_check("Rain is expected in Dunmere.", _spy)
    assert result.hold is False
    assert calls == []


def test_traces_carry_no_raw_values():
    raw_name = "Mara Vell"
    raw_dob = "14 March 1989"
    trace: list[dict] = []
    findings = tier1.scan_reid(DOB_TEXT, trace_out=trace)
    assert findings
    result = reid.assess(DOB_TEXT)
    reid.trace_combine(trace, result.score, result.combination,
                       result.outcome)
    residual = reid.ResidualResult(hold=True, verified_clues=[raw_name],
                                   can_identify=True)
    reid.trace_residual(trace, residual)
    assert [step["step"] for step in trace[:2]] == [
        "reid.extract", "reid.combine"]
    blob = "\n".join(str(step) for step in trace)
    assert raw_name not in blob
    assert raw_dob not in blob


def test_engine_gate_reid_warn_and_hold():
    policy, registry = _policy(), _registry()
    warn = engine.gate_reid(
        "The woman with the parrot tattoo at the harbour kiosk.",
        policy, registry)
    assert warn.decision == "WARN"
    assert warn.sent_text is not None
    assert warn.evidence["reid"]["banner"].startswith("Together, your ")
    assert engine.user_message(warn) == warn.evidence["reid"]["banner"]

    hold = engine.gate_reid(DOB_TEXT, policy, registry)
    assert hold.decision == "HOLD"
    assert hold.sent_text is None
    assert "name" in hold.evidence["reid"]["combination"]

    plain = engine.gate(DOB_TEXT, policy, registry)
    assert plain.decision == "HOLD"
    assert "name" in plain.evidence["reid"]["combination"]


def test_engine_gate_reid_conversation_and_residual():
    policy, registry = _policy(), _registry()
    decision = engine.gate_reid(
        "He just turned 60 last week.",
        policy, registry,
        conversation=["My neighbour works down at the harbour.",
                      "He is the only pilot-boat captain there."])
    assert decision.decision == "HOLD"

    judge = _fake_judge({
        "can_identify": True, "can_locate": True,
        "clues": ["1988", "Dunmere", "invented phrase"],
    })
    held = engine.gate_reid(
        "Mara Vell, born 1988, of 9 Bellflower Lane, Dunmere, "
        "retired Friday.",
        policy, registry, residual=True, judge_fn=judge)
    assert held.decision == "HOLD"
    assert held.evidence["reid"]["residual"]["verified"] == 2
    assert "[NAME]" not in str(held.evidence)


def test_weights_and_thresholds_documented():
    assert reid.WEIGHTS["dob"] >= reid.WEIGHTS["city"]
    assert reid.WEIGHTS["street"] >= reid.WEIGHTS["postcode"]
    assert reid.WARN_THRESHOLD < reid.HOLD_THRESHOLD
    assert frozenset({"name", "dob"}) in reid.HIGH_RISK_COMBOS
    assert frozenset({"name", "street"}) in reid.HIGH_RISK_COMBOS
    assert frozenset({"dob", "postcode", "gender"}) in reid.HIGH_RISK_COMBOS
    assert (frozenset({"employer", "jobtitle", "city"})
            in reid.HIGH_RISK_COMBOS)
