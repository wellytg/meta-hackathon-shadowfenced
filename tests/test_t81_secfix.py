"""T81 security-review fixes: one focused test per fix (1-7).

Fake secrets are always built at runtime from fragments (never written
as one literal) so tests/test_repo_guard.py stays green.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from shadowfenced import app as app_module


def _hosted(monkeypatch):
    monkeypatch.setenv("SF_HOSTED", "1")


# --- Fix 1: hosted /api/log/events exposes no decision ids --------------------


def test_hosted_log_events_exposes_no_decision_ids(monkeypatch):
    _hosted(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        app_module.log_events(50)
    assert caught.value.status_code == 403
    assert "decision_id" not in str(caught.value.detail)


def test_local_log_events_still_lists(monkeypatch):
    monkeypatch.delenv("SF_HOSTED", raising=False)
    body = app_module.log_events(50)
    assert body["ok"] is True
    assert body["events"] == []


# --- Fix 4: POST /api/provider refused in hosted mode -------------------------


def test_hosted_set_provider_refused(monkeypatch):
    _hosted(monkeypatch)
    before = app_module.providers()["default"]
    with pytest.raises(HTTPException) as caught:
        app_module.set_provider(app_module.ProviderRequest(id="meta"))
    assert caught.value.status_code == 403
    assert app_module.providers()["default"] == before
    app_module.reset_provider_override()


def test_local_set_provider_still_works(monkeypatch):
    monkeypatch.delenv("SF_HOSTED", raising=False)
    try:
        body = app_module.set_provider(app_module.ProviderRequest(id="meta"))
        assert body["default"] == "meta"
    finally:
        app_module.reset_provider_override()


# --- shared upstream mock ----------------------------------------------------


@pytest.fixture()
def mock_upstream():
    import httpx

    calls: list = []

    def make(answer: str = "ok") -> list:
        def handler(request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": answer}}]},
            )

        app_module.app.state.upstream_transport = httpx.MockTransport(handler)
        return calls

    yield make
    if hasattr(app_module.app.state, "upstream_transport"):
        delattr(app_module.app.state, "upstream_transport")


def _upstream_messages(calls) -> list:
    import json

    return json.loads(calls[0].content.decode("utf-8"))["messages"]


# --- Fix 3: /v1 forwards only safe fields; string parts rewritten -------------


def test_v1_forwards_only_safe_message_fields(mock_upstream):
    calls = mock_upstream()
    response = app_module.chat_completions(
        app_module.ChatRequest(
            model="x",
            messages=[
                {
                    "role": "user",
                    "content": "hello",
                    "name": "bob",
                    "function_call": {"name": "x", "arguments": "{}"},
                    "extra": "drop-me",
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "tool result",
                    "name": "evil",
                },
            ],
        )
    )
    assert response.status_code == 200
    forwarded = _upstream_messages(calls)
    assert forwarded == [
        {"role": "user", "content": "hello"},
        {"role": "tool", "tool_call_id": "call_1", "content": "tool result"},
    ]


def test_v1_rewrites_plain_string_content_parts(mock_upstream):
    calls = mock_upstream("noted [PII_EMAIL_1]")
    response = app_module.chat_completions(
        app_module.ChatRequest(
            model="x",
            messages=[
                {
                    "role": "user",
                    "content": [
                        "mail ada@corvanelabs.com now",
                        {"type": "text", "text": "nothing sensitive"},
                    ],
                },
            ],
        )
    )
    assert response.status_code == 200
    forwarded = _upstream_messages(calls)
    user_msg = next(m for m in forwarded if m.get("role") == "user")
    assert user_msg["content"][0] == "mail [PII_EMAIL_1] now"
    assert user_msg["content"][1] == {"type": "text", "text": "nothing sensitive"}
    import json

    assert "ada@corvanelabs.com" not in json.dumps(forwarded)


# --- Fix 2: /api/send gates every role ----------------------------------------


def _fake_cloud_key() -> str:
    # Runtime-built AWS-shaped key (never one literal in this file).
    return "AK" + "IA" + "A" * 16


def test_send_blocks_key_in_system_message(mock_upstream):
    calls = mock_upstream()
    key = _fake_cloud_key()
    body = app_module.send(
        app_module.SendRequest(
            messages=[
                {"role": "system", "content": f"the deploy key is {key} ok"},
                {"role": "user", "content": "hello"},
            ]
        )
    )
    assert body["decision"] == "BLOCK"
    assert body["sent_text"] is None
    assert body["answer"] is None
    assert calls == []


def test_send_holds_ambiguous_tool_message(mock_upstream):
    calls = mock_upstream()
    body = app_module.send(
        app_module.SendRequest(
            messages=[
                {"role": "user", "content": "run it"},
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "maybe aB3xK9qW2mZ7pL5sT8vN4yU6cE",
                },
            ]
        )
    )
    assert body["decision"] == "HOLD"
    assert body["sent_text"] is None
    assert body["answer"] is None
    assert calls == []


def test_send_blocks_key_in_user_tool_arguments(mock_upstream):
    calls = mock_upstream()
    key = _fake_cloud_key()
    body = app_module.send(
        app_module.SendRequest(
            messages=[
                {
                    "role": "user",
                    "content": "run it",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"key": "' + key + '"}',
                            },
                        }
                    ],
                },
            ]
        )
    )
    assert body["decision"] == "BLOCK"
    assert calls == []


def test_send_redacts_email_in_system_message(mock_upstream):
    calls = mock_upstream("noted [PII_EMAIL_1], thanks")
    body = app_module.send(
        app_module.SendRequest(
            messages=[
                {"role": "system", "content": "cc bob@corvanelabs.com always"},
                {"role": "user", "content": "hello"},
            ]
        )
    )
    assert body["decision"] == "REDACT"
    forwarded = _upstream_messages(calls)
    system_msgs = [m for m in forwarded if m.get("role") == "system"]
    assert any("[PII_EMAIL_1]" in str(m.get("content")) for m in system_msgs)
    import json

    assert "bob@corvanelabs.com" not in json.dumps(forwarded)
    assert body["answer"] == "noted bob@corvanelabs.com, thanks"


def test_send_merges_placeholders_across_roles(mock_upstream):
    calls = mock_upstream("mailed [PII_EMAIL_1] and [PII_EMAIL_2]")
    body = app_module.send(
        app_module.SendRequest(
            messages=[
                {"role": "system", "content": "cc bob@corvanelabs.com always"},
                {"role": "user", "content": "mail ada@corvanelabs.com now"},
            ]
        )
    )
    assert body["decision"] == "REDACT"
    forwarded = _upstream_messages(calls)
    import json

    dumped = json.dumps(forwarded)
    assert "ada@corvanelabs.com" not in dumped
    assert "bob@corvanelabs.com" not in dumped
    assert "[PII_EMAIL_1]" in dumped and "[PII_EMAIL_2]" in dumped
    assert body["answer"] == "mailed ada@corvanelabs.com and bob@corvanelabs.com"


# --- Fix 5: loopback-only console + trusted host -------------------------------

ROOT = Path(__file__).resolve().parents[1]


def _loop_available() -> bool:
    import asyncio
    import contextlib
    import io

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


requires_loop = pytest.mark.skipif(
    not _loop_available(), reason="no event loop: socket creation is forbidden here"
)


def test_compose_binds_console_to_loopback():
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert '"127.0.0.1:8765:8765"' in text
    assert '"8765:8765"' not in text.replace('"127.0.0.1:8765:8765"', "")


def test_dockerfile_keeps_container_bind_with_comment():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '"--host", "0.0.0.0"' in text
    assert "127.0.0.1:8765:8765" in text


def test_trusted_host_middleware_configured_locally():
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    assert app_module.LOCAL_TRUSTED_HOSTS == ["127.0.0.1", "localhost", "testserver"]
    entries = [
        entry
        for entry in app_module.app.user_middleware
        if isinstance(getattr(entry, "cls", None), type)
        and issubclass(entry.cls, TrustedHostMiddleware)
    ]
    assert entries, "no TrustedHostMiddleware on the app"
    assert entries[0].kwargs["allowed_hosts"] == ["127.0.0.1", "localhost", "testserver"]


@requires_loop
def test_trusted_host_rejects_rebinding_locally(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.delenv("SF_HOSTED", raising=False)
    client = TestClient(app_module.app, base_url="http://evil.example")
    response = client.get("/api/health")
    assert response.status_code == 400


@requires_loop
def test_trusted_host_allows_public_host_in_hosted_mode(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SF_HOSTED", "1")
    client = TestClient(app_module.app, base_url="http://demo.example.com")
    response = client.get("/api/hosted")
    assert response.status_code == 200
    assert response.json() == {
        "hosted": True,
        "banner": "Public demo. Use made-up data only.",
    }


# --- Fix 6: upload size proven before the body --------------------------------


def _drive(coro):
    """Drive a never-suspending coroutine without an event loop."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("route unexpectedly suspended")


def test_upload_limit_for_picks_cap(monkeypatch):
    from shadowfenced.extract import MAX_AUDIO_BYTES, MAX_FILE_BYTES

    from shadowfenced import hosted as hosted_module

    monkeypatch.delenv("SF_HOSTED", raising=False)
    assert app_module._upload_limit_for("clip.wav") == MAX_AUDIO_BYTES
    assert app_module._upload_limit_for("doc.txt") == MAX_FILE_BYTES
    monkeypatch.setenv("SF_HOSTED", "1")
    assert app_module._upload_limit_for("doc.txt") == hosted_module.HOSTED_MAX_FILE_BYTES
    assert app_module._upload_limit_for("clip.wav") == MAX_AUDIO_BYTES


def test_upload_declared_oversize_skips_body_read():
    from shadowfenced.extract import MAX_FILE_BYTES

    class ExplodingBody:
        size = MAX_FILE_BYTES + 5

        def read(self, _n=-1):
            raise AssertionError("body must not be read")

    stand_in = app_module._upload_oversize_stand_in(ExplodingBody(), MAX_FILE_BYTES)
    assert stand_in is not None and len(stand_in) == MAX_FILE_BYTES + 1

    class SmallBody:
        size = 10

        def read(self, _n=-1):
            raise AssertionError("checked separately")

    assert app_module._upload_oversize_stand_in(SmallBody(), MAX_FILE_BYTES) is None

    class UnknownSize:
        size = None

    assert app_module._upload_oversize_stand_in(UnknownSize(), MAX_FILE_BYTES) is None


def test_read_upload_capped_bounds_read():
    import io

    from shadowfenced.extract import MAX_FILE_BYTES

    buf = io.BytesIO(b"a" * (MAX_FILE_BYTES + 100))

    class FakeUpload:
        size = None

        async def read(self, n=-1):
            return buf.read(n)

    data = _drive(app_module._read_upload_capped(FakeUpload(), MAX_FILE_BYTES))
    assert len(data) == MAX_FILE_BYTES + 1
    assert buf.tell() == MAX_FILE_BYTES + 1


def test_read_upload_capped_honours_declared_oversize():
    from shadowfenced.extract import MAX_FILE_BYTES

    class FakeUpload:
        size = MAX_FILE_BYTES + 1

        async def read(self, _n=-1):
            raise AssertionError("body must not be read")

    data = _drive(app_module._read_upload_capped(FakeUpload(), MAX_FILE_BYTES))
    assert len(data) == MAX_FILE_BYTES + 1


def test_ext_scan_file_reads_at_most_cap_plus_one(monkeypatch):
    import io

    from starlette.datastructures import UploadFile as StarletteUploadFile

    from shadowfenced import ext_api, fileguard

    monkeypatch.delenv("SF_HOSTED", raising=False)
    fileguard.reset_for_tests()
    try:
        buf = io.BytesIO(b"a" * (fileguard.EXT_SCAN_MAX_BYTES + 100))
        upload = StarletteUploadFile(file=buf, filename="big.txt")
        payload = _drive(ext_api.ext_scan_file(upload, None))
        assert payload["decision"] == "UNSCANNABLE"
        assert "25 MB" in payload["why"]
        assert buf.tell() == fileguard.EXT_SCAN_MAX_BYTES + 1
    finally:
        fileguard.reset_for_tests()


def test_ext_scan_file_declared_oversize_skips_body_read(monkeypatch):
    import io

    from starlette.datastructures import UploadFile as StarletteUploadFile

    from shadowfenced import ext_api, fileguard

    monkeypatch.delenv("SF_HOSTED", raising=False)
    fileguard.reset_for_tests()
    try:

        class ExplodingBody(io.RawIOBase):
            def read(self, _n=-1):
                raise AssertionError("body must not be read")

        upload = StarletteUploadFile(
            file=ExplodingBody(),
            filename="big.txt",
            size=fileguard.EXT_SCAN_MAX_BYTES + 1,
        )
        payload = _drive(ext_api.ext_scan_file(upload, None))
        assert payload["decision"] == "UNSCANNABLE"
        assert "25 MB" in payload["why"]
    finally:
        fileguard.reset_for_tests()


@requires_loop
def test_upload_route_rejects_oversize_file(monkeypatch):
    from fastapi.testclient import TestClient

    from shadowfenced import uploads
    from shadowfenced.extract import MAX_FILE_BYTES

    monkeypatch.delenv("SF_HOSTED", raising=False)
    uploads.reset_for_tests()
    try:
        client = TestClient(app_module.app)
        response = client.post(
            "/api/upload",
            files={"file": ("big.txt", b"a" * (MAX_FILE_BYTES + 100))},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "unscannable"
        assert "exceeds" in body["error"]
        small = client.post("/api/upload", files={"file": ("ok.txt", b"water the plants")})
        assert small.json()["status"] == "clean"
    finally:
        uploads.reset_for_tests()


# --- Fix 7: no rehydration inside URLs ------------------------------------------


def _email_mapping():
    return {"[PII_EMAIL_1]": "ada@corvanelabs.com"}


def test_rehydrate_leaves_placeholder_in_image_target():
    from shadowfenced.redact import rehydrate

    answer = "see ![x](https://evil.example/?d=[PII_EMAIL_1]) now"
    text, highlights, unmatched = rehydrate(answer, _email_mapping())
    assert text == answer
    assert "ada@corvanelabs.com" not in text
    assert unmatched == []
    assert len(highlights) == 1
    note = highlights[0]
    assert note["placeholder"] == "[PII_EMAIL_1]"
    assert "link target" in note["note"]
    assert text[note["start"]:note["end"]] == "[PII_EMAIL_1]"


def test_rehydrate_leaves_placeholder_in_link_target():
    from shadowfenced.redact import rehydrate

    answer = "write to [a](mailto:[PII_EMAIL_1]) soon"
    text, highlights, unmatched = rehydrate(answer, _email_mapping())
    assert text == answer
    assert "ada@corvanelabs.com" not in text
    assert unmatched == []
    assert len(highlights) == 1
    assert highlights[0]["placeholder"] == "[PII_EMAIL_1]"
    assert "link target" in highlights[0]["note"]


def test_rehydrate_leaves_placeholder_in_bare_url():
    from shadowfenced.redact import rehydrate

    answer = "open https://evil.example/?d=[PII_EMAIL_1] today"
    text, highlights, unmatched = rehydrate(answer, _email_mapping())
    assert text == answer
    assert "ada@corvanelabs.com" not in text
    assert unmatched == []
    assert len(highlights) == 1
    assert highlights[0]["placeholder"] == "[PII_EMAIL_1]"
    assert "URL" in highlights[0]["note"]


def test_rehydrate_restores_prose_alongside_url():
    from shadowfenced.redact import rehydrate

    answer = "mail [PII_EMAIL_1] or open https://evil.example/?d=[PII_EMAIL_1]"
    text, highlights, unmatched = rehydrate(answer, _email_mapping())
    assert text == (
        "mail ada@corvanelabs.com or open https://evil.example/?d=[PII_EMAIL_1]"
    )
    assert unmatched == []
    assert len(highlights) == 2
    assert "note" not in highlights[0]
    assert text[highlights[0]["start"]:highlights[0]["end"]] == "ada@corvanelabs.com"
    assert "note" in highlights[1]


def test_rehydrate_restores_link_display_text():
    from shadowfenced.redact import rehydrate

    answer = "see [mail [PII_EMAIL_1] here](https://x.example) now"
    text, highlights, unmatched = rehydrate(answer, _email_mapping())
    assert text == "see [mail ada@corvanelabs.com here](https://x.example) now"
    assert unmatched == []
    assert len(highlights) == 1
    assert "note" not in highlights[0]


def test_rehydrate_leaves_bare_name_in_url():
    from shadowfenced.redact import rehydrate

    answer = "open https://evil.example/?d=PII_EMAIL_1 today"
    text, highlights, unmatched = rehydrate(answer, _email_mapping())
    assert text == answer
    assert "ada@corvanelabs.com" not in text
    assert unmatched == []
    assert len(highlights) == 1
    assert highlights[0]["placeholder"] == "[PII_EMAIL_1]"
    assert "note" in highlights[0]
