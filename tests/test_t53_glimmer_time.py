"""T53: local Glimmer checks finish on a 12 GB GPU instead of timing out.

One shared text-call timeout (``SF_GLIMMER_TIMEOUT_S``, default 45 s),
short answers (smallest schema, sized ``num_predict``, trimmed prompts,
capped user text), and skip rules so one send never fires duplicate
Glimmer calls when an earlier result already decides the outcome.

No test touches the network: every Ollama call goes through an httpx
MockTransport (timeouts are observed by spying on ``httpx.Client``).
Every person, place and value below is invented for the test.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from shadowfenced import engine, holds
from shadowfenced import judge as judge_module
from shadowfenced import person_search as person_search_module
from shadowfenced import reid as reid_module
from shadowfenced.canary import CanaryRegistry
from shadowfenced.config import get_settings
from shadowfenced.glimmer import DEFAULT_GLIMMER_TIMEOUT_S, glimmer_timeout_s
from shadowfenced.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
SEED = "test-seed-t53"
KINDS = ("db_password", "api_key")

DANA = (
    "Find everything you can about Dana Whitlock, born 14 March 1991, "
    "who lives on Copperleaf Drive in Fairhaven."
)
ADA = "Summarise the life of Ada Lovelace, born 10 December 1815 in London."


def _policy():
    return load_policy(ROOT / "policy.yaml")


def _registry():
    return CanaryRegistry(SEED, KINDS)


def _transport_for(content, calls=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(
            status, json={"message": {"role": "assistant", "content": content}}
        )

    return httpx.MockTransport(handler)


def _spy_timeout(monkeypatch) -> dict:
    seen: dict = {}
    real_client = httpx.Client

    def spy(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", spy)
    return seen


def _judgement_payload(**overrides) -> dict:
    payload = {
        "cls": "benign",
        "is_beacon": False,
        "sensitivity": 0.1,
        "confidence": 0.9,
        "span": "",
    }
    payload.update(overrides)
    return payload


def _private_search(text):
    return {
        "person_search": True,
        "subject_public": False,
        "subject": "Dana Whitlock",
        "inferred": ["Dana Whitlock", "born 14 March 1991"],
        "confidence": 0.9,
    }


def _public_figure(text):
    name = "Ada Lovelace" if "Ada Lovelace" in text else text.split(",")[0]
    return {
        "person_search": False,
        "subject_public": True,
        "subject": name,
        "inferred": [name],
        "confidence": 0.9,
    }


# --- one shared timeout -------------------------------------------------------

def test_shared_timeout_default_is_45():
    assert DEFAULT_GLIMMER_TIMEOUT_S == 45.0
    assert get_settings({}).glimmer_timeout_s == 45.0
    assert glimmer_timeout_s(get_settings({})) == 45.0


def test_shared_timeout_parsed_from_env(monkeypatch):
    monkeypatch.setenv("SF_GLIMMER_TIMEOUT_S", "33")
    assert get_settings().glimmer_timeout_s == 33.0
    assert glimmer_timeout_s() == 33.0


def test_shared_timeout_rejects_bad_values(monkeypatch):
    for bad in ("", "nope"):
        monkeypatch.setenv("SF_GLIMMER_TIMEOUT_S", bad)
        assert get_settings().glimmer_timeout_s == 45.0
    for bad in ("0", "-5"):
        monkeypatch.setenv("SF_GLIMMER_TIMEOUT_S", bad)
        assert glimmer_timeout_s(get_settings()) == 45.0


def test_old_judge_timeout_setting_is_gone():
    assert not hasattr(get_settings({}), "judge_timeout_s")


# --- the timeout is honoured by every text call -------------------------------

def test_judge_timeout_uses_shared_setting(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("SF_GLIMMER_TIMEOUT_S", "33")
    transport = _transport_for(json.dumps(_judgement_payload()))
    assert (
        judge_module.judge("window text", "m", live=False,
                           transport=transport) is not None
    )
    seen = _spy_timeout(monkeypatch)
    judge_module.judge("window text", "m", live=False, transport=transport)
    assert seen["timeout"] == 33.0


def test_judge_live_hints_keep_live_timeout(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("SF_LIVE_TIMEOUT_S", "7")
    monkeypatch.setenv("SF_GLIMMER_TIMEOUT_S", "33")
    seen = _spy_timeout(monkeypatch)
    transport = _transport_for(json.dumps(_judgement_payload()))
    judge_module.judge("window text", "m", live=True, transport=transport)
    assert seen["timeout"] == 7.0


def _live_transport(content, calls=None):
    return _transport_for(content, calls)


def test_person_search_timeout_uses_shared_setting(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("SF_GLIMMER_TIMEOUT_S", "33")
    seen = _spy_timeout(monkeypatch)
    calls: list = []
    transport = _live_transport(json.dumps(_private_search("")), calls)
    raw = person_search_module.local_glimmer_person_search(
        DANA, transport=transport
    )
    assert raw is not None
    assert seen["timeout"] == 33.0


def test_residual_timeout_uses_shared_setting(monkeypatch):
    monkeypatch.setenv("SF_GLIMMER_TIMEOUT_S", "33")
    seen = _spy_timeout(monkeypatch)
    content = json.dumps(
        {"can_identify": True, "can_locate": False, "clues": ["x"]}
    )
    raw = reid_module.local_glimmer_judge(
        "some redacted text", transport=_transport_for(content)
    )
    assert raw is not None
    assert seen["timeout"] == 33.0


def test_public_figure_timeout_uses_shared_setting(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("SF_GLIMMER_TIMEOUT_S", "33")
    seen = _spy_timeout(monkeypatch)
    content = json.dumps({"public_figure": True})
    verdict = reid_module.local_glimmer_public_figure(
        ADA, transport=_transport_for(content)
    )
    assert verdict is True
    assert seen["timeout"] == 33.0


def test_explicit_timeout_still_wins(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    seen = _spy_timeout(monkeypatch)
    transport = _transport_for(json.dumps(_private_search("")))
    person_search_module.local_glimmer_person_search(
        DANA, timeout_s=9.0, transport=transport
    )
    assert seen["timeout"] == 9.0
    seen.clear()
    content = json.dumps(
        {"can_identify": False, "can_locate": False, "clues": []}
    )
    reid_module.local_glimmer_judge(
        "some redacted text", timeout_s=9.0,
        transport=_transport_for(content),
    )
    assert seen["timeout"] == 9.0


# --- short answers: schema, num_predict, prompt, caps --------------------------

def test_person_search_schema_is_minimal_and_sized(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    calls: list = []
    transport = _transport_for(json.dumps(_private_search("")), calls)
    person_search_module.local_glimmer_person_search(DANA, transport=transport)
    assert len(calls) == 1
    body = json.loads(calls[0].content.decode("utf-8"))
    schema = body["format"]
    assert set(schema["properties"]) == {
        "person_search", "subject_public", "subject", "inferred",
        "confidence", "intent",
    }
    assert body["options"]["num_predict"] == 120
    assert body["options"]["temperature"] == 0
    assert "up to 5" in body["messages"][0]["content"]


def test_person_search_inferred_capped_at_five():
    judgement = person_search_module.PersonSearchJudgement(
        person_search=True,
        subject_public=False,
        subject="Dana Whitlock",
        inferred=[f"fact {index}" for index in range(7)],
        confidence=0.9,
    )
    assert len(judgement.inferred) == 5


def test_person_search_user_text_capped(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    calls: list = []
    transport = _transport_for(json.dumps(_private_search("")), calls)
    long_text = "Find Dana Whitlock, born 14 March 1991. " + "x" * 3000
    person_search_module.local_glimmer_person_search(
        long_text, transport=transport
    )
    body = json.loads(calls[0].content.decode("utf-8"))
    sent = body["messages"][1]["content"]
    assert len(sent) <= person_search_module.PERSON_SEARCH_TEXT_CHARS == 1000
    assert sent == long_text[-1000:]


def test_person_search_prompt_is_trimmed():
    prompt = person_search_module.PERSON_SEARCH_SYSTEM
    assert "up to 5 short facts" in prompt
    assert len(prompt) < 450


def test_residual_body_is_sized_and_capped(monkeypatch):
    calls: list = []
    content = json.dumps(
        {"can_identify": False, "can_locate": False, "clues": []}
    )
    reid_module.local_glimmer_judge(
        "Mara Vell, born 1988. " + "y" * 3000,
        transport=_transport_for(content, calls),
    )
    body = json.loads(calls[0].content.decode("utf-8"))
    assert body["options"]["num_predict"] == 120
    assert body["options"]["temperature"] == 0
    sent = body["messages"][0]["content"]
    assert "up to 5" in sent
    assert len(sent) < 1400
    redacted_tail = ("Mara Vell, born 1988. " + "y" * 3000)[
        -reid_module.RESIDUAL_TEXT_CHARS:
    ]
    assert sent.endswith(redacted_tail)


def test_public_figure_body_is_tiny_and_capped(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    calls: list = []
    reid_module.local_glimmer_public_figure(
        ADA + " z" * 3000, transport=_transport_for(
            json.dumps({"public_figure": True}), calls
        ),
    )
    body = json.loads(calls[0].content.decode("utf-8"))
    assert body["options"]["num_predict"] == 20
    sent = body["messages"][1]["content"]
    assert len(sent) <= reid_module.PUBLIC_FIGURE_TEXT_CHARS == 500


def test_judge_num_predict_unchanged(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    calls: list = []
    transport = _transport_for(json.dumps(_judgement_payload()), calls)
    judge_module.judge("window text", "m", live=False, transport=transport)
    body = json.loads(calls[0].content.decode("utf-8"))
    assert body["options"]["num_predict"] == 80


def test_message_review_judges_first_1000_chars(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    calls: list = []
    transport = _transport_for(json.dumps(_judgement_payload()), calls)
    long_text = "w" * 2500
    hold = holds.Hold(
        hold_id="h", text=long_text, messages=[], provider=None, spans=[],
    )
    item = {"kind": "message", "text": long_text}
    suspicions: list = []
    confirmed: list = []
    holds._review_message_item(
        hold, item, long_text, [], "m", transport,
        _policy(), _registry(), suspicions, confirmed,
    )
    assert suspicions == []
    body = json.loads(calls[0].content.decode("utf-8"))
    judged = body["messages"][1]["content"]
    assert judged == judge_module.quote_as_data(
        long_text[: holds.MESSAGE_JUDGE_CHARS]
    )
    assert long_text[:1000] in judged


# --- skip logic: fixed order, earlier verdicts decide ---------------------------

def test_residual_hold_skips_person_search():
    calls: list = []

    def _holding_judge(_text):
        return {
            "can_identify": True,
            "can_locate": True,
            "clues": ["1988", "Dunmere"],
        }

    def _ps_spy(_text):
        calls.append(_text)
        return _private_search(_text)

    text = "Mara Vell, born 1988, of 9 Bellflower Lane, Dunmere, retired Friday."
    decision = engine.gate(
        text, _policy(), _registry(), residual=True,
        judge_fn=_holding_judge, person_search_judge_fn=_ps_spy,
    )
    assert decision.decision == "HOLD"
    assert decision.evidence["reid"]["residual"]["verified"] == 2
    assert calls == []


def test_no_trigger_means_no_person_search_call(monkeypatch):
    calls: list = []

    def _counting(_text):
        calls.append(_text)
        return None

    monkeypatch.setattr(
        person_search_module, "local_glimmer_person_search", _counting
    )
    decision = engine.gate(
        "The office printer is on the third floor.", _policy(), _registry()
    )
    assert decision.decision == "ALLOW"
    assert calls == []


def test_pure_cue_hold_with_verified_public_figure_allows():
    trace: list = []
    decision = engine.gate(
        ADA, _policy(), _registry(), mode="send",
        person_search_judge_fn=_public_figure, trace_out=trace,
    )
    assert decision.decision == "ALLOW"
    assert decision.sent_text == ADA
    assert any(
        step["step"] == "intent.public_figure" and step["result"] == "OK"
        for step in trace
    )


def test_pure_cue_hold_stays_held_when_glimmer_fails():
    decision = engine.gate(
        ADA, _policy(), _registry(), mode="send",
        person_search_judge_fn=lambda _text: None,
    )
    assert decision.decision == "HOLD"
    assert decision.sent_text is None


def test_public_figure_downgrade_needs_encyclopedic_framing():
    text = "Ada Lovelace, born 10 December 1815, hid the password."
    decision = engine.gate(
        text, _policy(), _registry(), mode="send",
        person_search_judge_fn=_public_figure,
    )
    assert decision.decision == "HOLD"


# --- UI status names the local checker after 2 s --------------------------------

def test_status_names_glimmer_after_two_seconds():
    js = (ROOT / "src" / "shadowfenced" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "Muse Glimmer is checking on this PC" in js
    assert ", 2000);" in js
    html = (ROOT / "src" / "shadowfenced" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "Checking locally" in html
