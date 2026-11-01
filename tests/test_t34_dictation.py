"""Voice dictation tests (T34/T39): POST /api/transcribe plus static checks.

The Muse Voice Transcribe call is faked with an injected httpx
``MockTransport``: no test touches the network or sends real audio.
Static checks pin the consent-first UI contract of ``dictation.js``
and its in-browser WAV encoding.
"""

from __future__ import annotations

import io
import json
import re
import struct
import wave
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from shadowfenced import app as app_module
from shadowfenced.app import app, handle_transcribe

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"


def make_wav(seconds: float = 0.1) -> bytes:
    """Pure-Python encoder helper: mono 16-bit 16 kHz PCM WAV bytes."""
    import math

    rate = 16000
    n = max(1, int(seconds * rate))
    frames = b"".join(
        struct.pack("<h", int(10000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(n)
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames)
    return buf.getvalue()


def make_wav_sized(total_bytes: int) -> bytes:
    """Valid mono 16-bit 16 kHz WAV of exactly ``total_bytes`` bytes."""
    n = (total_bytes - 44) // 2
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * n)
    out = buf.getvalue()
    assert len(out) == total_bytes
    return out


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    app_module.reset_state()
    monkeypatch.setenv("META_API_KEY", "test-key")
    monkeypatch.delenv("SF_HOSTED", raising=False)
    monkeypatch.delenv("SF_HOSTED_DICTATION", raising=False)
    yield
    app_module.reset_state()
    if hasattr(app.state, "transcribe_transport"):
        delattr(app.state, "transcribe_transport")
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")


def _transcribe_transport(transcript: str, seen: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"transcript": transcript})

    return httpx.MockTransport(handler)


def _watch_upstream(calls: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("transcribe must never call a chat model")

    return httpx.MockTransport(handler)


# --- endpoint ---------------------------------------------------------------


def test_route_registered():
    table = {
        getattr(r, "path", ""): sorted(getattr(r, "methods", []) or [])
        for r in app.routes
    }
    assert table["/api/transcribe"] == ["POST"]


def test_transcribe_returns_text():
    seen: list[httpx.Request] = []
    app.state.transcribe_transport = _transcribe_transport("hello world", seen)
    body = handle_transcribe("clip.wav", make_wav())
    assert body == {"text": "hello world"}
    assert seen and seen[0].url.path.endswith("/v1/asr/transcribe")


def test_transcribe_posts_asr_multipart():
    seen: list[httpx.Request] = []
    app.state.transcribe_transport = _transcribe_transport("hello", seen)
    body = handle_transcribe("clip.wav", make_wav())
    assert body == {"text": "hello"}
    request = seen[0]
    assert request.url.path.endswith("/v1/asr/transcribe")
    assert request.headers.get("accept") == "application/json"
    assert (request.headers.get("authorization") or "").startswith("Bearer ")
    content = request.content
    assert content.count(b"Content-Disposition: form-data;") == 2
    assert b'name="request"' in content
    assert b'name="audio"' in content
    assert b"application/json" in content
    assert b"audio/wav" in content
    marker = content.index(b'name="request"')
    head, _, rest = content[marker:].partition(b"\r\n\r\n")
    assert b"application/json" in head
    payload, _, _ = rest.partition(b"\r\n--")
    assert json.loads(payload.decode("utf-8")) == {
        "model": "muse-voice-transcribe-1.0",
        "audioEncoding": "WAV",
        "mode": "PUSH_TO_TALK",
    }


def test_transcribe_never_forwards_to_chat_model():
    upstream_calls: list[httpx.Request] = []
    app.state.upstream_transport = _watch_upstream(upstream_calls)
    seen: list[httpx.Request] = []
    app.state.transcribe_transport = _transcribe_transport("hello", seen)
    body = handle_transcribe("clip.wav", make_wav(), "3")
    assert body == {"text": "hello"}
    assert upstream_calls == []


def test_transcribe_size_cap():
    seen: list[httpx.Request] = []
    app.state.transcribe_transport = _transcribe_transport("hello", seen)
    big = b"a" * (5 * 1024 * 1024 + 1)
    with pytest.raises(HTTPException) as caught:
        handle_transcribe("clip.wav", big)
    assert caught.value.status_code == 413
    assert caught.value.detail["code"] == "shadowfenced_audio_too_large"
    assert "text" not in caught.value.detail
    assert seen == []


def test_transcribe_at_size_cap_ok():
    seen: list[httpx.Request] = []
    app.state.transcribe_transport = _transcribe_transport("ok", seen)
    exact = make_wav_sized(5 * 1024 * 1024)
    body = handle_transcribe("clip.wav", exact)
    assert body == {"text": "ok"}
    assert len(seen) == 1


def test_transcribe_duration_cap():
    seen: list[httpx.Request] = []
    app.state.transcribe_transport = _transcribe_transport("hello", seen)
    with pytest.raises(HTTPException) as caught:
        handle_transcribe("clip.wav", make_wav(), "61")
    assert caught.value.status_code == 413
    assert caught.value.detail["code"] == "shadowfenced_audio_too_long"
    assert "text" not in caught.value.detail
    assert seen == []


def test_transcribe_duration_boundary_ok():
    seen: list[httpx.Request] = []
    app.state.transcribe_transport = _transcribe_transport("hello", seen)
    body = handle_transcribe("clip.wav", make_wav(), "60")
    assert body == {"text": "hello"}
    assert len(seen) == 1


def test_transcribe_unsupported_type():
    seen: list[httpx.Request] = []
    app.state.transcribe_transport = _transcribe_transport("hello", seen)
    with pytest.raises(HTTPException) as caught:
        handle_transcribe("clip.txt", make_wav())
    assert caught.value.status_code == 415
    assert seen == []


def test_transcribe_empty_clip():
    seen: list[httpx.Request] = []
    app.state.transcribe_transport = _transcribe_transport("hello", seen)
    with pytest.raises(HTTPException) as caught:
        handle_transcribe("clip.wav", b"")
    assert caught.value.status_code == 400
    assert "text" not in caught.value.detail
    assert seen == []


def test_transcribe_error_path_has_no_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    app.state.transcribe_transport = httpx.MockTransport(handler)
    with pytest.raises(HTTPException) as caught:
        handle_transcribe("clip.wav", make_wav())
    assert caught.value.status_code == 502
    assert caught.value.detail["code"] == "shadowfenced_transcribe_failed"
    assert "text" not in caught.value.detail


def test_transcribe_timeout_has_no_text():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow network")

    app.state.transcribe_transport = httpx.MockTransport(handler)
    with pytest.raises(HTTPException) as caught:
        handle_transcribe("clip.wav", make_wav())
    assert caught.value.status_code == 502
    assert "text" not in caught.value.detail


def test_transcribe_hosted_default_off(monkeypatch):
    monkeypatch.setenv("SF_HOSTED", "1")
    seen: list[httpx.Request] = []
    app.state.transcribe_transport = _transcribe_transport("hello", seen)
    with pytest.raises(HTTPException) as caught:
        handle_transcribe("clip.wav", make_wav())
    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "shadowfenced_dictation_disabled"
    assert seen == []


def test_transcribe_hosted_opt_in(monkeypatch):
    monkeypatch.setenv("SF_HOSTED", "1")
    monkeypatch.setenv("SF_HOSTED_DICTATION", "1")
    seen: list[httpx.Request] = []
    app.state.transcribe_transport = _transcribe_transport("hello", seen)
    body = handle_transcribe("clip.wav", make_wav())
    assert body == {"text": "hello"}
    assert len(seen) == 1


# --- static checks ----------------------------------------------------------


def _static(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_dictation_js_targets_composer_tools():
    js = _static("dictation.js")
    assert 'getElementById("composer-tools")' in js
    assert "paperclip" in js  # fallback: next to the Attach button


def test_dictation_js_shows_notice_before_first_use():
    js = _static("dictation.js")
    assert "Dictation uses Meta Muse Voice in the cloud." in js
    assert "Your voice is sent to Meta to be turned into text." in js
    assert "The text is then checked here before it goes to any AI." in js
    assert "Turn on dictation" in js
    assert "Cancel" in js
    assert "hasConsent" in js


def test_dictation_js_stores_only_consent_flag():
    js = _static("dictation.js")
    assert "sf-dictation-consent" in js
    for match in re.finditer(
        r"localStorage\.(setItem|getItem|removeItem)\(\s*([^,)]+)", js
    ):
        key = match.group(2).strip()
        assert key in ("CONSENT_KEY", '"sf-dictation-consent"'), key
    # Audio bytes and transcripts are never persisted.
    assert js.count("setItem") == 1
    assert "sessionStorage" not in js
    assert "indexedDB" not in js


def test_dictation_js_records_and_inserts_at_cursor():
    js = _static("dictation.js")
    assert 'data-testid", "mic-btn"' in js
    assert 'aria-label", "Dictate"' in js
    assert "MediaRecorder" in js
    assert "60" in js  # 60-second stop
    assert "setRangeText" in js
    assert 'dispatchEvent(new Event("input"' in js
    assert "/api/send" not in js  # nothing is sent automatically
    assert "/api/transcribe" in js


def test_dictation_js_encodes_wav_in_browser():
    js = _static("dictation.js")
    # 16-bit PCM WAV header: RIFF/WAVE container, 1 channel, 16000 Hz.
    assert "RIFF" in js
    assert "WAVE" in js
    assert "16000" in js
    assert "1 channel" in js
    # Web Audio pipeline: record, decode, downmix/resample, upload WAV.
    assert "decodeAudioData" in js
    assert "OfflineAudioContext" in js
    assert "dictation.wav" in js
    assert "audio/wav" in js


def test_dictation_js_handles_mic_errors_and_motion():
    js = _static("dictation.js")
    assert "NotAllowedError" in js
    assert "getUserMedia" in js
    assert "No microphone" in js
    css = _static("dictation.css")
    assert "prefers-reduced-motion" in css


def test_dictation_tags_in_index_html():
    html = _static("index.html")
    assert html.count('href="dictation.css"') == 1
    assert html.count('src="dictation.js"') == 1
