"""Audio transcription via Meta's Muse Voice Transcribe (T39).

Real API (dev.meta.ai/docs/speech-to-text,
dev.meta.ai/docs/api-reference/voice/transcribe, cookbook
``06_muse_voice/01_voice_api_fundamentals``):

- ``POST {META_BASE_URL}/asr/transcribe`` (the base URL defaults to
  ``https://api.meta.ai/v1``, so this is ``/v1/asr/transcribe``) with
  headers ``Authorization: Bearer <Meta key>`` and
  ``Accept: application/json``.
- ``multipart/form-data`` with exactly two parts: ``request`` = JSON
  ``{"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV",
  "mode": "PUSH_TO_TALK"}`` sent as ``application/json``, and ``audio``
  = the WAV file (``audio/wav``).
- Audio must be a RIFF/WAVE file, mono, 16-bit PCM, 16 kHz or 24 kHz
  (this module always normalises to mono 16-bit 16 kHz). Max 32 MB
  (HTTP 413), max 10 minutes (HTTP 400); 429 = rate limited;
  500 = processing failure.
- Response JSON: top-level ``transcript`` holds the text (``turns`` is
  empty in ``PUSH_TO_TALK``).

Uploads in other formats (mp3/m4a/ogg/webm) are converted to mono
16-bit 16 kHz WAV on the server before sending: PCM WAV input is
decoded with the standard :mod:`wave` module (pure Python, no
dependency); anything else goes through ``ffmpeg`` when it is on
``PATH``, otherwise transcription fails closed with a message telling
the user to install ffmpeg or upload a WAV file.

Every failure (missing key, oversize file, missing ffmpeg, network
error, timeout, bad status, bad payload, empty transcript) raises
:class:`TranscribeError` so callers fail closed (the upload becomes
``unscannable`` and the send HOLDs). No audio bytes are ever written
to disk or the log.
"""

from __future__ import annotations

import array
import io
import json
import shutil
import struct
import subprocess
import wave

import httpx

from .config import Settings, get_settings
from .upstream import _verify_for

TRANSCRIBE_MODEL = "muse-voice-transcribe-1.0"

#: Path appended to ``META_BASE_URL`` (default ``https://api.meta.ai/v1``).
TRANSCRIBE_PATH = "/asr/transcribe"

#: Exact JSON body of the ``request`` multipart part.
REQUEST_PAYLOAD: dict[str, str] = {
    "model": TRANSCRIBE_MODEL,
    "audioEncoding": "WAV",
    "mode": "PUSH_TO_TALK",
}

TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1
TARGET_SAMPWIDTH = 2

MAX_AUDIO_BYTES = 32 * 1024 * 1024

TIMEOUT_S = 60.0

#: Accepted status codes and their fail-closed messages.
STATUS_MESSAGES: dict[int, str] = {
    400: "Recording too long",
    413: "Audio too large",
    429: "Muse Voice is busy, try again",
    500: "Muse Voice could not process this audio",
}

# suffix -> MIME type (accepted upload formats; all are normalised to
# mono 16-bit 16 kHz WAV before sending).
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


def encode_wav_mono16(frames_pcm16: bytes, sample_rate: int = TARGET_SAMPLE_RATE) -> bytes:
    """Wrap little-endian 16-bit mono frames in a RIFF/WAVE container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(TARGET_CHANNELS)
        wf.setsampwidth(TARGET_SAMPWIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(frames_pcm16)
    return buf.getvalue()


def _decode_wav(data: bytes) -> tuple[int, list[float]]:
    """Decode a WAV file to ``(sample_rate, mono float samples)``.

    Handles PCM widths 8/16/24/32-bit and downmixes multi-channel
    input to mono. Raises :class:`TranscribeError` for anything that
    is not readable PCM WAV.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            params = wf.getparams()
            raw = wf.readframes(params.nframes)
    except TranscribeError:
        raise
    except Exception as exc:
        raise TranscribeError("audio file is not a readable WAV file") from exc
    if params.comptype != "NONE":
        raise TranscribeError("audio file uses compressed WAV; upload a PCM WAV file")
    nchannels, sampwidth, framerate = (
        params.nchannels,
        params.sampwidth,
        params.framerate,
    )
    if framerate <= 0:
        raise TranscribeError("audio file is not a readable WAV file")
    if sampwidth == 1:
        vals = [(b - 128) / 128.0 for b in raw]
    elif sampwidth == 2:
        count = len(raw) // 2
        vals = [v / 32768.0 for v in struct.unpack("<%dh" % count, raw[: count * 2])]
    elif sampwidth in (3, 4):
        step = sampwidth
        scale = float(1 << (8 * step - 1))
        vals = [
            int.from_bytes(raw[i : i + step], "little", signed=True) / scale
            for i in range(0, len(raw) - step + 1, step)
        ]
    else:
        raise TranscribeError(f"unsupported WAV sample width: {sampwidth * 8} bits")
    if nchannels > 1:
        usable = (len(vals) // nchannels) * nchannels
        vals = [
            sum(vals[i : i + nchannels]) / nchannels
            for i in range(0, usable, nchannels)
        ]
    if not vals:
        raise TranscribeError("audio file is empty")
    return framerate, vals


def _resample_mono(samples: list[float], src_rate: int) -> list[float]:
    """Linear-resample mono float samples to 16 kHz (no-op when equal)."""
    if src_rate == TARGET_SAMPLE_RATE or not samples:
        return samples
    ratio = src_rate / TARGET_SAMPLE_RATE
    out_len = max(1, int(round(len(samples) / ratio)))
    last = len(samples) - 1
    out = [0.0] * out_len
    for i in range(out_len):
        pos = i * ratio
        j = int(pos)
        if j >= last:
            out[i] = samples[last]
            continue
        frac = pos - j
        out[i] = samples[j] + (samples[j + 1] - samples[j]) * frac
    return out


def _to_pcm16_bytes(mono: list[float]) -> bytes:
    """Quantise float samples in [-1, 1] to little-endian int16."""
    arr = array.array("h")
    for v in mono:
        q = int(round(v * 32767.0))
        if q > 32767:
            q = 32767
        elif q < -32768:
            q = -32768
        arr.append(q)
    return arr.tobytes()


def _normalise_wav(data: bytes) -> bytes:
    """Return mono 16-bit 16 kHz WAV bytes for WAV input.

    Input already in that exact format passes through untouched.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            params = wf.getparams()
    except Exception as exc:
        raise TranscribeError("audio file is not a readable WAV file") from exc
    if (
        params.comptype == "NONE"
        and params.nchannels == TARGET_CHANNELS
        and params.sampwidth == TARGET_SAMPWIDTH
        and params.framerate == TARGET_SAMPLE_RATE
    ):
        return data
    framerate, mono = _decode_wav(data)
    return encode_wav_mono16(_to_pcm16_bytes(_resample_mono(mono, framerate)))


def _convert_with_ffmpeg(data: bytes) -> bytes:
    """Convert non-WAV audio to mono 16-bit 16 kHz WAV via ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise TranscribeError(
            "Install ffmpeg to transcribe this format, or upload a WAV file"
        )
    try:
        proc = subprocess.run(
            [
                exe,
                "-y",
                "-v",
                "error",
                "-i",
                "pipe:0",
                "-ac",
                "1",
                "-ar",
                str(TARGET_SAMPLE_RATE),
                "-sample_fmt",
                "s16",
                "-f",
                "wav",
                "pipe:1",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TranscribeError(
            f"audio conversion failed: {type(exc).__name__}"
        ) from exc
    if proc.returncode != 0 or not proc.stdout:
        raise TranscribeError("could not convert audio; upload a WAV file")
    return _normalise_wav(proc.stdout)


def prepare_wav(filename: str, data: bytes) -> tuple[str, bytes]:
    """Normalise one upload to ``(wav_name, mono 16-bit 16 kHz WAV)``.

    Raises :class:`TranscribeError` when the suffix is unsupported,
    the file is empty or oversize, no converter is available, or the
    audio cannot be decoded.
    """
    _mime_for(filename)  # validates the suffix (raises TranscribeError).
    if not data:
        raise TranscribeError("audio file is empty")
    if len(data) > MAX_AUDIO_BYTES:
        raise TranscribeError(f"audio file exceeds {MAX_AUDIO_BYTES} bytes")
    if filename.lower().endswith(".wav"):
        return filename, _normalise_wav(data)
    wav_bytes = _convert_with_ffmpeg(data)
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return stem + ".wav", wav_bytes


def _parse_transcript(response: httpx.Response) -> str:
    """Extract the transcript text or raise a mapped TranscribeError."""
    if response.status_code != 200:
        message = STATUS_MESSAGES.get(
            response.status_code, f"transcription status {response.status_code}"
        )
        raise TranscribeError(message)
    try:
        payload = response.json()
    except ValueError as exc:
        raise TranscribeError("transcription returned bad JSON") from exc
    text = payload.get("transcript") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise TranscribeError("transcription returned no text")
    return text


def _request_parts(wav_name: str, wav_bytes: bytes) -> dict:
    """The exact two multipart parts: ``request`` JSON + ``audio`` WAV."""
    return {
        "request": (
            "request",
            json.dumps(REQUEST_PAYLOAD),
            "application/json",
        ),
        "audio": (wav_name, wav_bytes, "audio/wav"),
    }


def transcribe_audio_sync(
    filename: str,
    data: bytes,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = TIMEOUT_S,
) -> str:
    """Transcribe one audio file and return the transcript text.

    The upload is normalised to mono 16-bit 16 kHz WAV and sent to
    ``{META_BASE_URL}/asr/transcribe``. Raises :class:`TranscribeError`
    on any failure so the upload path can HOLD instead of passing
    unchecked audio. A test may inject an ``httpx`` transport
    (``MockTransport`` works with the sync client).
    """
    cfg = settings or get_settings()
    wav_name, wav_bytes = prepare_wav(filename, data)
    if not cfg.meta_available:
        raise TranscribeError("meta upstream unavailable")
    url = cfg.meta_base_url.rstrip("/") + TRANSCRIBE_PATH
    headers = {
        "Authorization": "Bearer " + cfg.meta_api_key,
        "Accept": "application/json",
    }
    try:
        with httpx.Client(
            transport=transport,
            timeout=timeout,
            verify=_verify_for(url),
        ) as client:
            response = client.post(
                url,
                headers=headers,
                files=_request_parts(wav_name, wav_bytes),
            )
    except TranscribeError:
        raise
    except Exception as exc:
        raise TranscribeError(
            f"transcription unreachable: {type(exc).__name__}"
        ) from exc
    return _parse_transcript(response)


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
    wav_name, wav_bytes = prepare_wav(filename, data)
    if not cfg.meta_available:
        raise TranscribeError("meta upstream unavailable")
    url = cfg.meta_base_url.rstrip("/") + TRANSCRIBE_PATH
    headers = {
        "Authorization": "Bearer " + cfg.meta_api_key,
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            verify=_verify_for(url),
        ) as client:
            response = await client.post(
                url,
                headers=headers,
                files=_request_parts(wav_name, wav_bytes),
            )
    except TranscribeError:
        raise
    except Exception as exc:
        raise TranscribeError(
            f"transcription unreachable: {type(exc).__name__}"
        ) from exc
    return _parse_transcript(response)
