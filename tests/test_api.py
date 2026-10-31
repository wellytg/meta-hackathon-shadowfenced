"""API + upstream tests (T5): scan, send, chat completions, health.

Secrets are built at runtime from the canary registry and no request
touches the network: upstream is an httpx MockTransport. The app
endpoints are plain ``def`` so tests call them directly with request
models; a route table test pins paths and methods. The two tests that
need a real event loop (async ``chat``, Starlette ``TestClient``) run
where loops exist and skip where socket creation is forbidden.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json

import httpx
import pytest
from fastapi import HTTPException

from shadowfenced import app as app_module
from shadowfenced.app import (
    ChatRequest,
    ScanRequest,
    SendRequest,
    app,
    chat_completions,
    health,
    scan,
    send,
)
from shadowfenced.canary import CanaryRegistry
from shadowfenced.config import Settings
from shadowfenced.upstream import (
    UpstreamError,
    build_request,
    chat_sync,
    parse_answer,
)

SEED = "test-seed-t5"
KINDS = ("db_password", "api_key")
ENTROPY_TOKEN = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"


def _loop_available() -> bool:
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            loop = asyncio.new_event_loop()
        except Exception:
            return False
    try:
        loop.close()
    except Exception:
        pass
    return True


LOOP_OK = _loop_available()
requires_loop = pytest.mark.skipif(
    not LOOP_OK, reason="no event loop: socket creation is forbidden here"
)


@pytest.fixture()
def mock_upstream():
    calls: list[httpx.Request] = []

    def make(answer: str = "happy to help", status: int = 200) -> list:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                status,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": answer}}
                    ]
                },
            )

        app.state.upstream_transport = httpx.MockTransport(handler)
        return calls

    yield make
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")


def _canary() -> str:
    return CanaryRegistry(SEED, KINDS).value("db_password")


def _upstream_json(calls) -> dict:
    return json.loads(calls[0].content.decode("utf-8"))


def _json_body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_routes_registered():
    table = {getattr(r, "path", ""): sorted(getattr(r, "methods", []) or []) for r in app.routes}
    assert table["/api/health"] == ["GET"]
    assert table["/api/scan"] == ["POST"]
    assert table["/api/send"] == ["POST"]
    assert table["/v1/chat/completions"] == ["POST"]


def test_health_ok():
    assert health() == {"status": "ok"}


@pytest.mark.parametrize("mode", ["tier1", "live"])
def test_scan_returns_spans_and_latency(mode):
    body = scan(ScanRequest(text="mail ada@corvanelabs.com now", mode=mode))
    assert any(s["subclass"] == "email" for s in body["spans"])
    span = next(s for s in body["spans"] if s["subclass"] == "email")
    for key in (
        "span_id", "start", "end", "cls", "subclass", "tier", "proof",
        "message", "why", "suggested_action", "can_keep", "confidence",
    ):
        assert key in span
    assert isinstance(span["start"], int) and isinstance(span["end"], int)
    expected = {"tier1", "total"} if mode == "tier1" else {"tier1", "tier2", "total"}
    assert set(body["latency_ms"]) == expected


def test_scan_clean_text():
    assert scan(ScanRequest(text="hello there"))["spans"] == []


def test_send_allow_forwards_text(mock_upstream):
    calls = mock_upstream("happy to help")
    body = send(SendRequest(messages=[{"role": "user", "content": "hello there"}]))
    assert body["decision"] == "ALLOW"
    assert body["sent_text"] == "hello there"
    assert body["answer"] == "happy to help"
    forwarded = _upstream_json(calls)
    assert forwarded["messages"][-1]["content"] == "hello there"
    assert calls[0].url.path == "/v1/chat/completions"


def test_send_redact_hides_value_and_rehydrates(mock_upstream):
    calls = mock_upstream("noted [PII_EMAIL_1], thanks")
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "mail ada@corvanelabs.com now"}]
        )
    )
    assert body["decision"] == "REDACT"
    assert body["sent_text"] == "mail [PII_EMAIL_1] now"
    forwarded_text = json.dumps(_upstream_json(calls))
    assert "ada@corvanelabs.com" not in forwarded_text
    assert body["answer"] == "noted ada@corvanelabs.com, thanks"
    assert body["answer_highlights"][0]["placeholder"] == "[PII_EMAIL_1]"
    assert body["unmatched_placeholders"] == []


def test_send_block_skips_upstream(mock_upstream):
    calls = mock_upstream()
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": f"password: {_canary()}"}]
        )
    )
    assert body["decision"] == "BLOCK"
    assert body["sent_text"] is None
    assert body["answer"] is None
    assert calls == []


def test_send_hold_ambiguous_skips_upstream(mock_upstream):
    calls = mock_upstream()
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": f"maybe {ENTROPY_TOKEN}"}]
        )
    )
    assert body["decision"] == "HOLD"
    assert body["sent_text"] is None
    assert body["answer"] is None
    assert calls == []


def test_send_upstream_error_502(mock_upstream):
    mock_upstream("nope", status=500)
    with pytest.raises(HTTPException) as exc_info:
        send(SendRequest(messages=[{"role": "user", "content": "hello"}]))
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "shadowfenced_upstream"


def test_send_keep_choice_allows_keepable_span(mock_upstream):
    mock_upstream()
    found = scan(ScanRequest(text="mail ada@corvanelabs.com now"))
    span_id = next(s["span_id"] for s in found["spans"] if s["subclass"] == "email")
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "mail ada@corvanelabs.com now"}],
            choices=[{"span_id": span_id, "choice": "keep"}],
        )
    )
    assert body["decision"] == "ALLOW"
    assert body["sent_text"] == "mail ada@corvanelabs.com now"


def test_chat_allow_200_with_header_and_field(mock_upstream):
    mock_upstream("hi back")
    response = chat_completions(
        ChatRequest(model="x", messages=[{"role": "user", "content": "hello"}])
    )
    assert response.status_code == 200
    assert response.headers["x-shadowfenced-decision"] == "ALLOW"
    body = _json_body(response)
    assert body["choices"][0]["message"]["content"] == "hi back"
    assert body["shadowfenced"]["decision"] == "ALLOW"
    assert body["shadowfenced"]["decision_id"]


def test_chat_redact_rehydrates(mock_upstream):
    mock_upstream("mail to [PII_EMAIL_1] sent")
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[{"role": "user", "content": "mail ada@corvanelabs.com now"}],
        )
    )
    assert response.status_code == 200
    assert response.headers["x-shadowfenced-decision"] == "REDACT"
    assert "ada@corvanelabs.com" in _json_body(response)["choices"][0]["message"]["content"]


def test_chat_block_403(mock_upstream):
    calls = mock_upstream()
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[{"role": "user", "content": f"password: {_canary()}"}],
        )
    )
    assert response.status_code == 403
    assert _json_body(response)["error"]["code"] == "shadowfenced_block"
    assert calls == []


def test_chat_hold_409_with_review_url(mock_upstream):
    calls = mock_upstream()
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[{"role": "user", "content": f"maybe {ENTROPY_TOKEN}"}],
        )
    )
    assert response.status_code == 409
    body = _json_body(response)
    assert body["error"]["code"] == "shadowfenced_hold"
    assert body["review_url"].startswith("/api/holds/")
    assert calls == []


def _stream_text(response) -> str:
    """Read an SSE body without an event loop.

    This Starlette wraps a sync iterator in an async generator, so
    unwrap one level via the generator frame instead of iterating.
    """
    iterator = response.body_iterator
    frame = getattr(iterator, "ag_frame", None)
    if frame is not None:
        for value in frame.f_locals.values():
            try:
                chunks = list(value)
            except TypeError:
                continue
            if chunks and all(isinstance(c, str) for c in chunks):
                return "".join(chunks)
    raise AssertionError("cannot read stream body without an event loop")


def test_chat_stream_single_chunk_then_done(mock_upstream):
    mock_upstream("streamed answer")
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    text = _stream_text(response)
    assert text.count("data: ") == 2
    assert text.strip().endswith("data: [DONE]")
    assert "streamed answer" in text


def test_chat_upstream_error_502(mock_upstream):
    mock_upstream("nope", status=500)
    response = chat_completions(
        ChatRequest(model="x", messages=[{"role": "user", "content": "hello"}])
    )
    assert response.status_code == 502
    assert _json_body(response)["error"]["code"] == "shadowfenced_upstream"


def test_chat_non_text_content_holds(mock_upstream):
    calls = mock_upstream()
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look at this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "http://example.com/a.png"},
                        },
                    ],
                }
            ],
        )
    )
    assert response.status_code == 409
    assert _json_body(response)["error"]["code"] == "shadowfenced_hold"
    assert calls == []


def test_send_non_text_content_holds(mock_upstream):
    calls = mock_upstream()
    body = send(
        SendRequest(
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "x"}}],
                }
            ]
        )
    )
    assert body["decision"] == "HOLD"
    assert body["sent_text"] is None
    assert calls == []


# --- upstream unit tests (no loop needed except the async path) --------------


def test_build_request_ollama_shape():
    url, headers, body = build_request(
        [{"role": "user", "content": "hi"}], None, Settings()
    )
    assert url == "http://127.0.0.1:11434/v1/chat/completions"
    assert headers == {}
    assert body["model"] == "llama3.2:3b"


def test_build_request_meta_shape_and_bearer():
    settings = Settings(
        upstream="meta",
        meta_api_key="k",
        meta_base_url="http://meta.example",
        meta_model="m",
    )
    url, headers, body = build_request(
        [{"role": "user", "content": "hi"}], "meta", settings
    )
    assert url == "http://meta.example/chat/completions"
    assert headers == {"Authorization": "Bearer k"}
    assert body["model"] == "m"


def test_build_request_meta_unavailable_and_unknown_provider():
    with pytest.raises(UpstreamError):
        build_request([], "meta", Settings(upstream="meta"))
    with pytest.raises(UpstreamError):
        build_request([], "nope", Settings())


def test_parse_answer_ok_and_bad_shapes():
    assert (
        parse_answer(200, {"choices": [{"message": {"content": "hi"}}]}) == "hi"
    )
    with pytest.raises(UpstreamError):
        parse_answer(500, {})
    with pytest.raises(UpstreamError):
        parse_answer(200, {"choices": []})


def test_chat_sync_full_path_over_mock_transport():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "sync hi"}}]},
        )

    answer = chat_sync(
        [{"role": "user", "content": "hi"}],
        "meta",
        settings=Settings(
            upstream="meta",
            meta_api_key="k",
            meta_base_url="http://meta.example",
            meta_model="m",
        ),
        transport=httpx.MockTransport(handler),
    )
    assert answer == "sync hi"
    assert seen["url"] == "http://meta.example/chat/completions"
    assert seen["auth"] == "Bearer k"


def test_chat_sync_unreachable_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(UpstreamError):
        chat_sync(
            [{"role": "user", "content": "hi"}],
            "ollama",
            settings=Settings(),
            transport=httpx.MockTransport(handler),
        )


@requires_loop
def test_async_chat_full_path_over_mock_transport():
    from shadowfenced.upstream import chat

    async def go() -> str:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]},
            )

        return await chat(
            [{"role": "user", "content": "hi"}],
            "ollama",
            settings=Settings(),
            transport=httpx.MockTransport(handler),
        )

    assert asyncio.run(go()) == "hi"


@requires_loop
def test_testclient_health_and_scan():
    from fastapi.testclient import TestClient

    client = TestClient(app)
    assert client.get("/api/health").json() == {"status": "ok"}
    body = client.post("/api/scan", json={"text": "hello"}).json()
    assert body["spans"] == []
    assert app_module.health() is not None
