"""Audio upload tests (T27/T39): transcripts scan like documents, fail closed.

The Muse Voice Transcribe call is faked with an injected httpx
``MockTransport``: no test touches the network or sends real audio.
"""

from __future__ import annotations

import io
import json
import wave

import httpx
import pytest

from shadowfenced import holds, uploads
from shadowfenced.app import SendRequest, app, handle_upload, reset_state, send
from shadowfenced.transcribe import (
    REQUEST_PAYLOAD,
    TRANSCRIBE_MODEL,
    TranscribeError,
    transcribe_audio_sync,
)


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


def make_wav(
    seconds: float = 0.1,
    sample_rate: int = 16000,
    channels: int = 1,
    freq: float = 440.0,
) -> bytes:
    """Pure-Python encoder helper: mono/16-bit/16 kHz PCM WAV bytes."""
    import math
    import struct

    n = max(1, int(seconds * sample_rate))
    frames = b"".join(
        struct.pack(
            "<" + "h" * channels,
            *(
                [int(10000 * math.sin(2 * math.pi * freq * i / sample_rate))]
                * channels
            ),
        )
        for i in range(n)
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(frames)
    return buf.getvalue()


def _transcribe_transport(transcript: str, seen: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"transcript": transcript})

    return httpx.MockTransport(handler)


def _request_part_body(content: bytes) -> dict:
    """Parse the ``request`` multipart part body back to a dict."""
    marker = b'name="request"'
    idx = content.index(marker)
    head, _, rest = content[idx:].partition(b"\r\n\r\n")
    assert b"application/json" in head
    body, _, _ = rest.partition(b"\r\n--")
    return json.loads(body.decode("utf-8"))


def assert_asr_request(request: httpx.Request) -> None:
    """Check the exact Muse Voice Transcribe multipart contract."""
    assert request.url.path.endswith("/v1/asr/transcribe")
    assert request.headers.get("accept") == "application/json"
    assert (request.headers.get("authorization") or "").startswith("Bearer ")
    content = request.content
    assert content.count(b'Content-Disposition: form-data; name="') == 2
    assert b'name="request"' in content
    assert b'name="audio"' in content
    assert TRANSCRIBE_MODEL.encode() in content
    assert b"audio/wav" in content
    assert _request_part_body(content) == REQUEST_PAYLOAD
    assert _request_part_body(content) == {
        "model": "muse-voice-transcribe-1.0",
        "audioEncoding": "WAV",
        "mode": "PUSH_TO_TALK",
    }


def test_audio_transcript_is_scanned_like_a_document(mock_upstream):
    calls = mock_upstream("noted, thanks")
    seen: list[httpx.Request] = []
    transport = _transcribe_transport("water the plants on fridays", seen)
    # The real /api/upload path resolves the transport from app state.
    app.state.transcribe_transport = transport
    uploaded = handle_upload("standup.wav", make_wav())
    assert uploaded["kind"] == "audio"
    assert uploaded["status"] == "clean"
    assert "Transcribed by Muse Voice" in uploaded["summary_text"]
    assert uploaded["chars"] > 0
    assert seen
    assert_asr_request(seen[0])

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
        "meeting.wav", make_wav(), transport=transport
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
        "note.wav", make_wav(), transport=httpx.MockTransport(handler)
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
        "note.wav", make_wav(), transport=httpx.MockTransport(handler)
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


# --- T39: real API contract -------------------------------------------------


def test_wav_helper_matches_mono_16bit_16khz():
    with wave.open(io.BytesIO(make_wav()), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.readframes(1)


def test_stereo_24khz_wav_is_normalised_before_send():
    seen: list[httpx.Request] = []
    text = transcribe_audio_sync(
        "note.wav",
        make_wav(sample_rate=24000, channels=2),
        transport=_transcribe_transport("hi", seen),
    )
    assert text == "hi"
    content = seen[0].content
    marker = content.index(b'name="audio"')
    head, _, rest = content[marker:].partition(b"\r\n\r\n")
    assert b"audio/wav" in head
    wav_bytes, _, _ = rest.partition(b"\r\n--")
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.readframes(1)


def test_request_goes_to_asr_transcribe_with_two_parts():
    seen: list[httpx.Request] = []
    text = transcribe_audio_sync(
        "note.wav", make_wav(), transport=_transcribe_transport("hi", seen)
    )
    assert text == "hi"
    assert seen
    assert_asr_request(seen[0])


def test_transcript_read_from_transcript_field():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"transcript": "hello", "turns": []}
        )

    text = transcribe_audio_sync(
        "note.wav", make_wav(), transport=httpx.MockTransport(handler)
    )
    assert text == "hello"


@pytest.mark.parametrize(
    "status,message",
    [
        (400, "Recording too long"),
        (413, "Audio too large"),
        (429, "Muse Voice is busy, try again"),
        (500, "Muse Voice could not process this audio"),
    ],
)
def test_status_codes_map_to_clear_messages(status, message):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "x"})

    with pytest.raises(TranscribeError) as caught:
        transcribe_audio_sync(
            "note.wav", make_wav(), transport=httpx.MockTransport(handler)
        )
    assert message in str(caught.value)


def test_foreign_format_without_ffmpeg_holds_send(monkeypatch, mock_upstream):
    calls = mock_upstream("noted")
    monkeypatch.setattr("shutil.which", lambda name: None)
    uploaded = handle_upload("song.mp3", b"ID3-fake-audio")
    assert uploaded["status"] == "unscannable"
    assert "ffmpeg" in (uploaded["error"] or "").lower()

    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "summarise this"}],
            file_ids=[uploaded["file_id"]],
        )
    )
    assert body["decision"] == "HOLD"
    assert body["sent_text"] is None
    assert calls == []
