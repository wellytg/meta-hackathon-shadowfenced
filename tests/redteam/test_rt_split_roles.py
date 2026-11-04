"""Red-team: keys split across messages and roles (T79).

The ``/v1/chat/completions`` proxy gates every role it forwards (system,
user, assistant, tool) plus assistant tool-call arguments -- each message on
its own. A key split into two halves in two messages is never seen whole,
so both halves forward and the model can join them.

Same story in ``/api/send``: the user messages join with a newline, but the
key shape never matches across the join, so both halves are sent.
"""

from __future__ import annotations

import json

import pytest

from shadowfenced.app import ChatRequest, SendRequest, chat_completions, send
from helpers_rt import aws_key, email, upstream_mock


@pytest.fixture()
def mock_upstream():
    yield from upstream_mock()


def _halves(value: str) -> tuple[str, str]:
    cut = len(value) // 2
    return value[:cut], value[cut:]


def _body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.parametrize("role", ["system", "user", "assistant", "tool"])
def test_whole_key_blocked_in_every_role(role, mock_upstream):
    """Control: a whole key in any single role is BLOCKed (403)."""
    calls = mock_upstream()
    response = chat_completions(
        ChatRequest(model="m", messages=[
            {"role": role, "content": f"deploy with {aws_key()} tonight"},
        ])
    )
    assert response.status_code == 403
    assert _body(response)["shadowfenced"]["decision"] == "BLOCK"
    assert calls == []


def test_key_in_tool_call_arguments_blocked(mock_upstream):
    """Control: a key inside assistant tool-call arguments is BLOCKed."""
    calls = mock_upstream()
    response = chat_completions(
        ChatRequest(model="m", messages=[
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"arguments": f"deploy={aws_key()}"}}]},
        ])
    )
    assert response.status_code == 403
    assert calls == []


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: proxy gates each message alone; halves rejoin upstream",
)
def test_key_split_across_two_user_messages_send(mock_upstream):
    """Halves of a key in two ``/api/send`` messages must not both be sent."""
    key = aws_key()
    first, second = _halves(key)
    calls = mock_upstream()
    body = send(SendRequest(messages=[
        {"role": "user", "content": f"first half {first}"},
        {"role": "user", "content": f"second half {second}"},
    ]))
    assert body["decision"] in ("BLOCK", "HOLD")
    assert calls == []


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: proxy gates each message alone; halves rejoin upstream",
)
def test_key_split_across_system_and_user_roles(mock_upstream):
    """Key halves in system + user messages must not both forward."""
    key = aws_key()
    first, second = _halves(key)
    calls = mock_upstream()
    response = chat_completions(
        ChatRequest(model="m", messages=[
            {"role": "system", "content": f"part one {first}"},
            {"role": "user", "content": f"part two {second}"},
        ])
    )
    assert response.status_code in (403, 409)
    assert calls == []


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: proxy gates each message alone; halves rejoin upstream",
)
def test_key_split_across_tool_and_user_roles(mock_upstream):
    """Key halves in tool + user messages must not both forward."""
    key = aws_key()
    first, second = _halves(key)
    calls = mock_upstream()
    response = chat_completions(
        ChatRequest(model="m", messages=[
            {"role": "tool", "content": f"tool output {first}"},
            {"role": "user", "content": f"remainder {second}"},
        ])
    )
    assert response.status_code in (403, 409)
    assert calls == []


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: proxy gates each message alone; halves rejoin upstream",
)
def test_email_split_across_two_turns(mock_upstream):
    """Email halves across two turns must not both forward in the clear."""
    mail = email()
    first, second = _halves(mail)
    calls = mock_upstream()
    response = chat_completions(
        ChatRequest(model="m", messages=[
            {"role": "user", "content": f"my mail starts {first}"},
            {"role": "user", "content": f"and ends {second}"},
        ])
    )
    assert response.status_code in (403, 409)
    assert calls == []
