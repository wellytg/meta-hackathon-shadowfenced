"""Audio upload tests (T27): transcripts scan like documents, fail closed.

The Muse Voice Transcribe call is faked with an injected httpx
``MockTransport``: no test touches the network or sends real audio.
"""

from __future__ import annotations

import httpx
import pytest

from shadowfenced import holds, uploads
from shadowfenced.app import SendRequest, app, handle_upload, reset_state, send
from shadowfenced.transcribe import TRANSCRIBE_MODEL


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    uploads.reset_for_tests()
    holds.reset_for_tests()
    reset_state()
    monkeypatch.setenv("META_API_KEY", "test-key")
    yield
    uploads.reset_for_tests()
    holds.reset_for_tests()
    reset_state()
    if hasattr(app.state, "transcribe_transport"):
        delattr(app.state, "transcribe_transport")
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")


@pytest.fixture()
def mock_upstream():
    calls: list[httpx.Request] = []

    def make(answer: str = "done") -> list:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
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


def _transcribe_transport(transcript: str, seen: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": transcript})

    return httpx.MockTransport(handler)


def test_audio_transcript_is_scanned_like_a_document(mock_upstream):
    calls = mock_upstream("noted, thanks")
    seen: list[httpx.Request] = []
    transport = _transcribe_transport("water the plants on fridays", seen)
    # The real /api/upload path resolves the transport from app state.
    app.state.transcribe_transport = transport
    uploaded = handle_upload("standup.webm", b"\x1a\x45\xdf\xa3-fake-audio")
    assert uploaded["kind"] == "audio"
    assert uploaded["status"] == "clean"
    assert "Transcribed by Muse Voice" in uploaded["summary_text"]
    assert uploaded["chars"] > 0
    assert seen and seen[0].url.path.endswith("/audio/transcriptions")
    assert TRANSCRIBE_MODEL.encode() in seen[0].content

    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "summarise this"}],
            file_ids=[uploaded["file_id"]],
        )
    )
    assert body["decision"] == "ALLOW"
    assert "water the plants" in (body["sent_text"] or "")
    assert calls != []


def test_spoken_canary_in_audio_blocks_send(mock_upstream):
    from shadowfenced.app import get_registry

    calls = mock_upstream("noted")
    canary = get_registry().value("db_password")
    seen: list[httpx.Request] = []
    transport = _transcribe_transport(
        f"the password is {canary}, do not share it", seen
    )
    uploaded = handle_upload(
        "meeting.mp3", b"ID3-fake-audio", transport=transport
    )
    assert uploaded["kind"] == "audio"
    assert uploaded["status"] == "flagged"

    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "summarise this"}],
            file_ids=[uploaded["file_id"]],
        )
    )
    assert body["decision"] == "BLOCK"
    assert body["sent_text"] is None
    assert body["answer"] is None
    assert calls == []


def test_transcription_error_holds_send(mock_upstream):
    calls = mock_upstream("noted")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    uploaded = handle_upload(
        "note.wav", b"RIFF-fake-audio", transport=httpx.MockTransport(handler)
    )
    assert uploaded["status"] == "unscannable"

    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "summarise this"}],
            file_ids=[uploaded["file_id"]],
        )
    )
    assert body["decision"] == "HOLD"
    assert body["sent_text"] is None
    assert body["answer"] is None
    assert body["review_url"].startswith("/api/holds/")
    assert calls == []


def test_transcription_timeout_holds_send(mock_upstream):
    calls = mock_upstream("noted")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow network")

    uploaded = handle_upload(
        "note.m4a", b"ftyp-fake-audio", transport=httpx.MockTransport(handler)
    )
    assert uploaded["status"] == "unscannable"

    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "summarise this"}],
            file_ids=[uploaded["file_id"]],
        )
    )
    assert body["decision"] == "HOLD"
    assert body["sent_text"] is None
    assert calls == []
