"""Agent gate keyguard tests (T22): every role gated, protected keys.

Fake protected values are set through the settings env override
(monkeypatch); the module keeps only salted HMACs, so nothing in logs
or responses may contain a value.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from shadowfenced import keyguard as keyguard_module
from shadowfenced.app import (
    ChatRequest,
    app,
    chat_completions,
    keyguard_status,
)
from shadowfenced.config import get_settings

FAKE_KEY = "FAKEKEY7H2sK9pQ4vX8mZ1aB3cD"


@pytest.fixture()
def fake_key(monkeypatch):
    """Protect a fake value via the settings env override."""
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    keyguard_module.reset()
    yield FAKE_KEY
    keyguard_module.reset()


@pytest.fixture()
def mock_upstream():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    app.state.upstream_transport = httpx.MockTransport(handler)
    yield calls
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")


def _json_body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def _log_text() -> str:
    with open(get_settings().log_path, encoding="utf-8") as handle:
        return handle.read()


def _log_records() -> list[dict]:
    return [
        json.loads(line)
        for line in _log_text().splitlines()
        if line.strip()
    ]


def test_direct_tool_message_blocked(fake_key, mock_upstream):
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[
                {"role": "user", "content": "please run the lookup"},
                {
                    "role": "tool",
                    "content": f"lookup result with {fake_key} inside",
                    "tool_call_id": "call_1",
                },
            ],
        )
    )
    assert response.status_code == 403
    body = _json_body(response)
    assert body["error"]["code"] == "shadowfenced_block"
    assert mock_upstream == []
    raw = response.body.decode("utf-8")
    assert fake_key not in raw
    assert fake_key not in _log_text()
    record = _log_records()[-1]
    assert record["decision"] == "BLOCK"
    assert record["protected_key"] == "META_API_KEY"
    assert record["role"] == "tool"
    assert record["left_machine"] is False


def test_base64_tool_message_blocked(fake_key, mock_upstream):
    blob = base64.b64encode(fake_key.encode()).decode()
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[
                {"role": "user", "content": "please run the lookup"},
                {"role": "tool", "content": f"blob {blob} end"},
            ],
        )
    )
    assert response.status_code == 403
    assert mock_upstream == []
    assert fake_key not in response.body.decode("utf-8")
    assert blob not in response.body.decode("utf-8")
    assert fake_key not in _log_text()
    assert blob not in _log_text()
    record = _log_records()[-1]
    assert record["protected_key"] == "META_API_KEY"
    assert record["left_machine"] is False


def test_split_tool_message_blocked(fake_key, mock_upstream):
    scattered = " ".join(fake_key)
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[
                {"role": "user", "content": "please run the lookup"},
                {"role": "tool", "content": f"chars {scattered} done"},
            ],
        )
    )
    assert response.status_code == 403
    assert mock_upstream == []
    assert fake_key not in response.body.decode("utf-8")
    assert fake_key not in _log_text()


def test_tool_call_arguments_blocked(fake_key, mock_upstream):
    decision, _items, _text = None, None, None
    from shadowfenced.app import _gate_chat_messages

    decision, _items, _text = _gate_chat_messages(
        [
            {"role": "user", "content": "run it"},
            {
                "role": "assistant",
                "content": "calling lookup",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": f'{{"key": "{fake_key}"}}',
                        },
                    }
                ],
            },
        ]
    )
    assert decision.decision == "BLOCK"
    assert any(
        "META_API_KEY appeared in a assistant message" in reason
        for reason in decision.reasons
    )
    assert any(
        span.cls == "secret" and span.subclass == "protected_key"
        for span in decision.spans
    )
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[
                {"role": "user", "content": "run it"},
                {
                    "role": "assistant",
                    "content": "calling lookup",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": f'{{"key": "{fake_key}"}}',
                            },
                        }
                    ],
                },
            ],
        )
    )
    assert response.status_code == 403
    assert mock_upstream == []
    assert fake_key not in _log_text()


def test_clean_order_and_roles_preserved(fake_key, mock_upstream):
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Summarize the quarterly report."},
        {
            "role": "assistant",
            "content": "I will look that up.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "arguments": '{"city": "Paris"}',
                    },
                }
            ],
        },
        {"role": "tool", "content": "lookup result sunny", "tool_call_id": "call_1"},
    ]
    response = chat_completions(ChatRequest(model="x", messages=messages))
    assert response.status_code == 200
    assert response.headers["x-shadowfenced-decision"] == "ALLOW"
    forwarded = json.loads(mock_upstream[0].content.decode("utf-8"))["messages"]
    assert [(m["role"], m["content"]) for m in forwarded] == [
        ("system", "You are a helpful assistant."),
        ("user", "Summarize the quarterly report."),
        ("assistant", "I will look that up."),
        ("tool", "lookup result sunny"),
    ]
    tool_calls = forwarded[2]["tool_calls"]
    assert tool_calls[0]["function"]["arguments"] == '{"city": "Paris"}'


def test_redact_preserves_order_and_roles(fake_key, mock_upstream):
    calls = mock_upstream

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "mail to [PII_EMAIL_1] sent",
                        }
                    }
                ]
            },
        )

    app.state.upstream_transport = httpx.MockTransport(handler)
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "mail ada@corvanelabs.com now"},
            ],
        )
    )
    assert response.status_code == 200
    forwarded = json.loads(calls[-1].content.decode("utf-8"))["messages"]
    assert forwarded[0]["role"] == "system"
    assert forwarded[1] == {"role": "system", "content": "Be brief."}
    assert forwarded[2]["role"] == "user"
    assert "ada@corvanelabs.com" not in forwarded[2]["content"]
    assert "[PII_EMAIL_1]" in forwarded[2]["content"]
    body = _json_body(response)
    assert "ada@corvanelabs.com" in body["choices"][0]["message"]["content"]


def test_keyguard_endpoint_counts(fake_key, mock_upstream):
    blob = base64.b64encode(fake_key.encode()).decode()
    for content in (
        f"direct {fake_key}",
        f"encoded {blob}",
        " ".join(fake_key),
    ):
        response = chat_completions(
            ChatRequest(
                model="x",
                messages=[{"role": "tool", "content": content}],
            )
        )
        assert response.status_code == 403
    status = keyguard_status()
    names = (
        status["protected_keys"]
        if "protected_keys" in status
        else [entry["name"] for entry in status["keys"]]
    )
    assert "META_API_KEY" in names
    if "hits" in status and isinstance(status["hits"], dict):
        assert status["hits"]["META_API_KEY"] == 3
    else:
        entry = next(
            item for item in status["keys"] if item["name"] == "META_API_KEY"
        )
        assert entry["hits"] == 3
    assert fake_key not in json.dumps(status)
    assert blob not in json.dumps(status)


def test_hash_store_holds_no_value(fake_key):
    keyguard_module.find_in_text(f"prefix {fake_key} suffix")
    state = "".join(
        str(value)
        for key, value in vars(keyguard_module).items()
        if not key.startswith("__")
    )
    assert fake_key not in state
    assert fake_key not in "".join(keyguard_module._HASHES.values())
