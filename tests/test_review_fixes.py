"""Review-fix tests: REDACT system note, error ``type`` fields, main guard."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from shadowfenced import app as app_module
from shadowfenced.app import (
    REDACT_NOTE,
    ChatRequest,
    SendRequest,
    app,
    chat_completions,
    send,
)
from shadowfenced.canary import CanaryRegistry

SEED = "test-seed-review"
ENTROPY_TOKEN = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"


@pytest.fixture()
def mock_upstream():
    calls: list[httpx.Request] = []

    def make(answer: str = "noted [PII_EMAIL_1], thanks") -> list:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": answer}}]},
            )

        app.state.upstream_transport = httpx.MockTransport(handler)
        return calls

    yield make
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")


def _canary() -> str:
    return CanaryRegistry(SEED, ("db_password",)).value("db_password")


def _upstream_messages(calls) -> list:
    return json.loads(calls[0].content.decode("utf-8"))["messages"]


def test_redact_prepends_system_note_only_on_redact(mock_upstream):
    calls = mock_upstream()
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "mail ada@corvanelabs.com now"}]
        )
    )
    assert body["decision"] == "REDACT"
    forwarded = _upstream_messages(calls)
    assert forwarded[0] == {"role": "system", "content": REDACT_NOTE}
    assert forwarded[-1]["content"] == "mail [PII_EMAIL_1] now"
    assert "ada@corvanelabs.com" not in json.dumps(forwarded)

    mock_upstream("happy to help")
    send(SendRequest(messages=[{"role": "user", "content": "hello there"}]))
    # Same shared call list: the ALLOW request is the second entry.
    second = json.loads(calls[1].content.decode("utf-8"))["messages"]
    assert second == [{"role": "user", "content": "hello there"}]


def test_chat_redact_prepends_system_note(mock_upstream):
    calls = mock_upstream("mail to [PII_EMAIL_1] sent")
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[{"role": "user", "content": "mail ada@corvanelabs.com now"}],
        )
    )
    assert response.status_code == 200
    forwarded = _upstream_messages(calls)
    assert forwarded[0] == {"role": "system", "content": REDACT_NOTE}
    assert "ada@corvanelabs.com" not in json.dumps(forwarded)


def test_error_objects_carry_type(mock_upstream):
    calls = mock_upstream()
    block = chat_completions(
        ChatRequest(
            model="x",
            messages=[{"role": "user", "content": f"password: {_canary()}"}],
        )
    )
    assert block.status_code == 403
    error = json.loads(block.body.decode("utf-8"))["error"]
    assert error["type"] == error["code"] == "shadowfenced_block"
    assert calls == []

    hold = chat_completions(
        ChatRequest(
            model="x",
            messages=[{"role": "user", "content": f"maybe {ENTROPY_TOKEN}"}],
        )
    )
    assert hold.status_code == 409
    hold_error = json.loads(hold.body.decode("utf-8"))["error"]
    assert hold_error["type"] == hold_error["code"] == "shadowfenced_hold"


def test_send_upstream_error_detail_carries_type(mock_upstream):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    app.state.upstream_transport = httpx.MockTransport(handler)
    with pytest.raises(HTTPException) as exc_info:
        send(SendRequest(messages=[{"role": "user", "content": "hello"}]))
    assert exc_info.value.detail["type"] == exc_info.value.detail["code"]


def test_main_guard_present():
    source = Path(app_module.__file__).read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    assert callable(app_module.main)
