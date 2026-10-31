"""Tier 2 judge tests (T8): schema parsing, request shape, fail-closed.

No test touches the network: every Ollama call goes through an httpx
MockTransport, and Tier 2 defaults to off unless a test opts into live.
"""

from __future__ import annotations

import json

import httpx
import pytest

from shadowfenced import judge
from shadowfenced.judge import Judgement, make_window, warmup

WINDOW = "some context around an ambiguous span here"


@pytest.fixture()
def live_mode(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")


def _judgement_payload(**overrides) -> dict:
    payload = {
        "cls": "secret",
        "is_beacon": False,
        "sensitivity": 0.8,
        "confidence": 0.7,
        "span": "aB3xK9qW2mZ7pL5sT8vN4yU6cE",
    }
    payload.update(overrides)
    return payload


def _transport_for(content, calls=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(
            status, json={"message": {"role": "assistant", "content": content}}
        )

    return httpx.MockTransport(handler)


def test_valid_json_parsed(live_mode):
    transport = _transport_for(json.dumps(_judgement_payload()))
    result = judge.judge(WINDOW, "test-model", live=False, transport=transport)
    assert isinstance(result, Judgement)
    assert result.cls == "secret"
    assert result.sensitivity == 0.8
    assert result.span == "aB3xK9qW2mZ7pL5sT8vN4yU6cE"
    assert result.confidence == 0.7


def test_trailing_eot_stripped(live_mode):
    transport = _transport_for(json.dumps(_judgement_payload()) + "<|eot|>")
    result = judge.judge(WINDOW, "test-model", live=False, transport=transport)
    assert isinstance(result, Judgement)
    assert result.cls == "secret"


def test_invalid_json_returns_none(live_mode):
    transport = _transport_for("this is not json{{{")
    assert (
        judge.judge(WINDOW, "m", live=False, transport=transport) is None
    )


def test_wrong_schema_returns_none(live_mode):
    transport = _transport_for(json.dumps({"cls": "secret"}))
    assert (
        judge.judge(WINDOW, "m", live=False, transport=transport) is None
    )


@pytest.mark.parametrize(
    "override",
    [
        {"sensitivity": 2.0},
        {"confidence": -0.1},
        {"cls": "nonsense"},
        {"is_beacon": "maybe"},
        {"sensitivity": "high"},
    ],
)
def test_out_of_range_rejected(live_mode, override):
    transport = _transport_for(json.dumps(_judgement_payload(**override)))
    assert (
        judge.judge(WINDOW, "m", live=False, transport=transport) is None
    )


def test_timeout_returns_none(live_mode):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow model")

    result = judge.judge(
        WINDOW, "m", live=False, transport=httpx.MockTransport(handler)
    )
    assert result is None


def test_transport_error_returns_none(live_mode):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    result = judge.judge(
        WINDOW, "m", live=True, transport=httpx.MockTransport(handler)
    )
    assert result is None


def test_request_uses_format_schema_seed_ctx_keepalive(live_mode):
    calls: list[httpx.Request] = []
    transport = _transport_for(
        json.dumps(_judgement_payload()), calls=calls
    )
    assert judge.judge(WINDOW, "m", live=False, transport=transport) is not None
    assert len(calls) == 1
    request = calls[0]
    assert request.url.path == "/api/chat"
    body = json.loads(request.content.decode("utf-8"))
    assert body["model"] == "m"
    assert body["format"] == Judgement.model_json_schema()
    assert body["stream"] is False
    assert body["think"] is False
    assert body["keep_alive"] == -1
    assert body["options"]["temperature"] == 0
    assert body["options"]["seed"] == 7
    assert body["options"]["num_ctx"] == 2048
    assert "num_gpu" not in body["options"]


def test_live_request_pins_num_gpu(live_mode, monkeypatch):
    monkeypatch.setenv("SF_LIVE_NUM_GPU", "0")
    calls: list[httpx.Request] = []
    transport = _transport_for(
        json.dumps(_judgement_payload()), calls=calls
    )
    assert judge.judge(WINDOW, "m", live=True, transport=transport) is not None
    body = json.loads(calls[0].content.decode("utf-8"))
    assert body["options"]["num_gpu"] == 0


def test_timeouts_come_from_env(monkeypatch, live_mode):
    seen: dict = {}
    real_client = httpx.Client

    def spy(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", spy)
    transport = _transport_for(json.dumps(_judgement_payload()))
    monkeypatch.setenv("SF_LIVE_TIMEOUT_S", "4")
    monkeypatch.setenv("SF_JUDGE_TIMEOUT_S", "20")
    judge.judge(WINDOW, "m", live=True, transport=transport)
    assert seen["timeout"] == 4.0
    judge.judge(WINDOW, "m", live=False, transport=transport)
    assert seen["timeout"] == 20.0


def test_off_mode_returns_none_without_request(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "off")
    calls: list[httpx.Request] = []
    transport = _transport_for(
        json.dumps(_judgement_payload()), calls=calls
    )
    assert judge.judge(WINDOW, "m", live=False, transport=transport) is None
    assert calls == []


def test_schema_is_minimal_fully_required_and_range_free():
    schema = Judgement.model_json_schema()
    assert set(schema["properties"]) == {
        "cls", "is_beacon", "sensitivity", "confidence", "span",
    }
    assert set(schema["required"]) == {
        "cls", "is_beacon", "sensitivity", "confidence", "span",
    }
    assert schema["properties"]["cls"]["enum"] == [
        "secret", "pii", "business", "beacon", "benign",
    ]
    text = json.dumps(schema)
    for keyword in (
        "minimum", "maximum", "minLength", "maxLength",
        "exclusiveMinimum", "exclusiveMaximum",
    ):
        assert keyword not in text


def test_system_prompt_is_minimal():
    assert judge.SYSTEM_PROMPT == (
        "Classify text a user is about to send to an outside AI. "
        "beacon = reveals where secrets or personal data are stored "
        "or were moved. "
        "span = the shortest exact sensitive phrase copied from the text, "
        "at most 8 words, or empty. JSON only."
    )


def test_send_judge_asks_for_80_tokens(live_mode):
    calls: list[httpx.Request] = []
    transport = _transport_for(
        json.dumps(_judgement_payload()), calls=calls
    )
    assert judge.judge(WINDOW, "m", live=False, transport=transport) is not None
    body = json.loads(calls[0].content.decode("utf-8"))
    assert body["options"]["num_predict"] == 80


def test_send_timeout_defaults_to_60_seconds():
    from shadowfenced.config import get_settings

    assert get_settings({}).judge_timeout_s == 60.0


def test_replay_mode_reads_fixture(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "replay")
    window = (
        "replay window alpha: we moved the old keys out of "
        "config.py last spring"
    )
    result = judge.judge(window, "any-model", live=False)
    assert isinstance(result, Judgement)
    assert result.cls == "beacon"
    assert result.is_beacon is True
    benign = judge.judge(
        "replay window beta: banana bread with walnuts and honey",
        "any-model",
        live=False,
    )
    assert benign is not None and benign.cls == "benign"
    assert judge.judge("no such window", "any-model", live=False) is None


def test_make_window_centers_span_with_context():
    text = "A" * 500 + "SPAN" + "B" * 500
    window = make_window(text, 500, 504)
    assert window == "A" * 300 + "SPAN" + "B" * 300
    assert "SPAN" in window


def test_make_window_clamps_at_edges():
    text = "short text with SPAN inside"
    window = make_window(text, 16, 20)
    assert window == text


def test_warmup_pings_both_models(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("SF_LIVE_MODEL", "live-model-x")
    monkeypatch.setenv("SF_JUDGE_MODEL", "judge-model-y")
    calls: list[httpx.Request] = []
    transport = _transport_for("ok", calls=calls)
    warmup(transport=transport)
    models = [
        json.loads(call.content.decode("utf-8"))["model"] for call in calls
    ]
    assert models == ["live-model-x", "judge-model-y"]


def test_warmup_off_sends_nothing(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "off")
    calls: list[httpx.Request] = []
    warmup(transport=_transport_for("ok", calls=calls))
    assert calls == []


def test_startup_hook_registered():
    from shadowfenced.app import app, lifespan

    assert app.router.lifespan_context is lifespan


def test_long_span_is_kept(live_mode):
    span = "one two three four five six seven eight nine ten"
    transport = _transport_for(json.dumps(_judgement_payload(span=span)))
    result = judge.judge(WINDOW, "m", live=False, transport=transport)
    assert result is not None and result.span == span
