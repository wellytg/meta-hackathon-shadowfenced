"""Audio transcription via Meta's Muse Voice Transcribe (T27).

Public docs consulted (model id ``muse-voice-transcribe-1.0``):

- https://dev.meta.ai/docs/speech-to-text — speech-to-text guide, file and
  realtime transcription with the voice model.
- https://dev.meta.ai/docs/api-reference/voice — API reference for voice.
- https://github.com/meta-models/meta-model-cookbook — ``06_muse_voice/``
  recipes (``transcribe_file.py``, ``transcribe_stream.py``).

The full reference pages could not be fetched reliably from this
environment, and public reports describe more than one shape for this API
(an OpenAI-style ``/audio/transcriptions`` multipart endpoint, a
``POST https://api.meta.ai/v1/asr/transcribe`` endpoint, and a ``/v1``
realtime WebSocket). This module therefore implements the documented
fallback from the task: an OpenAI-compatible ``POST``
``{META_BASE_URL}/audio/transcriptions`` multipart upload (fields
``file`` + ``model``) with ``model=muse-voice-transcribe-1.0`` and a
``{"text": ...}`` JSON response. It reuses the existing Meta settings
(``META_API_KEY`` / ``META_BASE_URL``) and the truststore TLS handling
from :mod:`shadowfenced.upstream`.

Every failure (missing key, oversize file, network error, timeout, bad
status, bad payload, empty transcript) raises :class:`TranscribeError`
so callers fail closed (the upload becomes ``unscannable`` and the send
HOLDs). No audio bytes are ever written to disk or the log.
"""

from __future__ import annotations

import httpx

from .config import Settings, get_settings
from .upstream import _verify_for

TRANSCRIBE_MODEL = "muse-voice-transcribe-1.0"

MAX_AUDIO_BYTES = 10 * 1024 * 1024

TIMEOUT_S = 60.0

# suffix -> MIME type for the multipart upload.
AUDIO_SUFFIXES: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
}


class TranscribeError(RuntimeError):
    """Audio transcription failed; the caller must fail closed."""


def is_audio_file(name: str) -> bool:
    """True when the file name has a supported audio suffix."""
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in AUDIO_SUFFIXES)


def _mime_for(name: str) -> str:
    lower = name.lower()
    for suffix, mime in AUDIO_SUFFIXES.items():
        if lower.endswith(suffix):
            return mime
    raise TranscribeError(f"unsupported audio type: {name!r}")


def transcribe_audio_sync(
    filename: str,
    data: bytes,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = TIMEOUT_S,
) -> str:
    """Transcribe one audio file and return the transcript text.

    Raises :class:`TranscribeError` on any failure so the upload path
    can HOLD instead of passing unchecked audio. A test may inject an
    ``httpx`` transport (``MockTransport`` works with the sync client).
    """
    cfg = settings or get_settings()
    _mime_for(filename)  # validates the suffix (raises TranscribeError).
    if len(data) > MAX_AUDIO_BYTES:
        raise TranscribeError(f"audio file exceeds {MAX_AUDIO_BYTES} bytes")
    if not data:
        raise TranscribeError("audio file is empty")
    if not cfg.meta_available:
        raise TranscribeError("meta upstream unavailable")
    url = cfg.meta_base_url.rstrip("/") + "/audio/transcriptions"
    headers = {"Authorization": "Bearer " + cfg.meta_api_key}
    try:
        with httpx.Client(
            transport=transport,
            timeout=timeout,
            verify=_verify_for(url),
        ) as client:
            response = client.post(
                url,
                headers=headers,
                data={"model": TRANSCRIBE_MODEL},
                files={
                    "file": (
                        filename,
                        data,
                        _mime_for(filename),
                    )
                },
            )
    except TranscribeError:
        raise
    except Exception as exc:
        raise TranscribeError(
            f"transcription unreachable: {type(exc).__name__}"
        ) from exc
    if response.status_code != 200:
        raise TranscribeError(
            f"transcription status {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise TranscribeError("transcription returned bad JSON") from exc
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise TranscribeError("transcription returned no text")
    return text


async def transcribe_audio(
    filename: str,
    data: bytes,
    *,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = TIMEOUT_S,
) -> str:
    """Async variant of :func:`transcribe_audio_sync`."""
    cfg = settings or get_settings()
    _mime_for(filename)
    if len(data) > MAX_AUDIO_BYTES:
        raise TranscribeError(f"audio file exceeds {MAX_AUDIO_BYTES} bytes")
    if not data:
        raise TranscribeError("audio file is empty")
    if not cfg.meta_available:
        raise TranscribeError("meta upstream unavailable")
    url = cfg.meta_base_url.rstrip("/") + "/audio/transcriptions"
    headers = {"Authorization": "Bearer " + cfg.meta_api_key}
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            verify=_verify_for(url),
        ) as client:
            response = await client.post(
                url,
                headers=headers,
                data={"model": TRANSCRIBE_MODEL},
                files={
                    "file": (
                        filename,
                        data,
                        _mime_for(filename),
                    )
                },
            )
    except TranscribeError:
        raise
    except Exception as exc:
        raise TranscribeError(
            f"transcription unreachable: {type(exc).__name__}"
        ) from exc
    if response.status_code != 200:
        raise TranscribeError(
            f"transcription status {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise TranscribeError("transcription returned bad JSON") from exc
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise TranscribeError("transcription returned no text")
    return text
