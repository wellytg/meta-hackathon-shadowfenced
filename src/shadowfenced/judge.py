"""Tier 2 judge: a local model classifies an ambiguous span in context.

``judge`` sends one ambiguous span plus ~300 chars of context on each side
(never the whole document) to Ollama's native ``/api/chat`` endpoint with
a minimal pydantic JSON schema as ``format``. Any failure — timeout,
transport error, invalid JSON, schema violation — returns None so the
gate fails closed. ``SF_TIER2_MODE`` selects ``live`` (default),
``replay`` (fixture answers keyed by sha256 of the window, CI only), or
``off`` (always None).

The schema is deliberately minimal — ``{cls, is_beacon, sensitivity,
confidence, span}``, all required, no ``reason`` and no min/max keywords
— so Glimmer answers in ~30-50 tokens. Ranges are validated in pydantic
after parsing (reject, never clamp).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, field_validator

from .config import get_settings

# Eval runs set SF_KEEP_ALIVE=5m so back-to-back judgements do not pin
# GPU memory between cases; production keeps models resident with -1.
import os as _os


def _keep_alive() -> Any:
    """Ollama keep_alive: int seconds, duration string, or -1 (resident)."""
    raw = (_os.environ.get("SF_KEEP_ALIVE") or "-1").strip()
    if raw == "-1":
        return -1
    try:
        return int(raw)
    except ValueError:
        return raw

JudgementClass = Literal["secret", "pii", "business", "beacon", "benign"]

SYSTEM_PROMPT = (
    "Classify text a user is about to send to an outside AI. "
    "beacon = reveals where secrets or personal data are stored or were moved. "
    "span = the shortest exact sensitive phrase copied from the text, "
    "at most 8 words, or empty. JSON only."
)

WINDOW_RADIUS = 300

_REPLAY_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures"
    / "judge_replay.json"
)


class Judgement(BaseModel):
    """One model verdict on an ambiguous span (no free-text reason)."""

    cls: JudgementClass
    is_beacon: bool
    sensitivity: float
    confidence: float
    span: str

    @field_validator("sensitivity", "confidence")
    @classmethod
    def _unit_range(cls, value: float) -> float:
        """Reject out-of-range scores (never clamp)."""
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("score must be a number")
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError("score must be within 0..1")
        return float(value)

    @field_validator("span")
    @classmethod
    def _span_word_limit(cls, value: str) -> str:
        """Reject spans longer than 8 words (the prompt asks for <= 8)."""
        if not isinstance(value, str):
            raise ValueError("span must be a string")
        if len(value.split()) > 8:
            raise ValueError("span must be at most 8 words")
        return value


def tier2_mode() -> str:
    """Current Tier 2 mode: ``live`` (default) | ``replay`` | ``off``."""
    return get_settings().tier2_mode


def make_window(text: str, cp_start: int, cp_end: int,
                 radius: int = WINDOW_RADIUS) -> str:
    """Slice an ambiguous span plus ~300 chars of context on each side."""
    lo = max(0, cp_start - radius)
    hi = min(len(text), cp_end + radius)
    return text[lo:hi]


def _replay_lookup(window: str) -> Judgement | None:
    """Return the fixture judgement for one window, or None."""
    try:
        raw = json.loads(_REPLAY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    key = hashlib.sha256(window.encode("utf-8")).hexdigest()
    try:
        return Judgement.model_validate(raw[key])
    except (KeyError, TypeError, ValueError):
        return None


def _request_body(window: str, model: str, *, live: bool) -> dict[str, Any]:
    options: dict[str, Any] = {
        "temperature": 0,
        "seed": 7,
        "num_ctx": 2048,
        "num_predict": 80,
    }
    if live:
        options["num_gpu"] = get_settings().live_num_gpu
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": window},
        ],
        "format": Judgement.model_json_schema(),
        "stream": False,
        "think": False,
        "keep_alive": _keep_alive(),
        "options": options,
    }


def _parse_content(content: Any) -> Judgement | None:
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    if text.endswith("<|eot|>"):
        text = text[: -len("<|eot|>")].strip()
    try:
        return Judgement.model_validate_json(text)
    except ValueError:
        return None


SCREEN_CHARS = 1500


def screen_enabled() -> bool:
    """Whole-message screen switch: ``SF_SCREEN=on|off`` (default on)."""
    return get_settings().screen_enabled


def screen(
    message: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Judgement | None:
    """Screen a whole outbound message with the live CPU model.

    Sends ``message[:1500]`` to ``SF_LIVE_MODEL`` with the same minimal
    ``Judgement`` schema as :func:`judge`. Any failure — disabled,
    timeout, transport error, invalid JSON, schema violation — returns
    None so the gate decides fail-closed in code.
    """
    try:
        settings = get_settings()
        if not settings.screen_enabled or not message:
            return None
        window = message[:SCREEN_CHARS]
        url = settings.ollama_url.rstrip("/") + "/api/chat"
        body = _request_body(window, settings.live_model, live=True)
        with httpx.Client(
            transport=transport, timeout=settings.screen_timeout_s
        ) as client:
            response = client.post(url, json=body)
        try:
            payload = response.json()
        except ValueError:
            return None
        msg = payload.get("message", {}) if isinstance(payload, dict) else {}
        content = msg.get("content") if isinstance(msg, dict) else None
        return _parse_content(content)
    except Exception:
        return None


def judge(
    text_window: str,
    model: str,
    *,
    live: bool,
    transport: httpx.BaseTransport | None = None,
) -> Judgement | None:
    """Ask the local judge about one window; None on any failure."""
    try:
        mode = tier2_mode()
        if mode == "off" or not text_window:
            return None
        if mode == "replay":
            return _replay_lookup(text_window)
        settings = get_settings()
        timeout = (
            settings.live_timeout_s if live else settings.judge_timeout_s
        )
        url = settings.ollama_url.rstrip("/") + "/api/chat"
        body = _request_body(text_window, model, live=live)
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.post(url, json=body)
        try:
            payload = response.json()
        except ValueError:
            return None
        message = payload.get("message", {}) if isinstance(payload, dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        return _parse_content(content)
    except Exception:
        return None


def _ping(
    model: str, *, live: bool,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """One tiny request to warm a resident model; never raises."""
    try:
        settings = get_settings()
        url = settings.ollama_url.rstrip("/") + "/api/chat"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "ok"}],
            "stream": False,
            "think": False,
            "keep_alive": _keep_alive(),
            "options": {"num_ctx": 2048, "num_predict": 4},
        }
        if live:
            body["options"]["num_gpu"] = settings.live_num_gpu
        with httpx.Client(transport=transport, timeout=60.0) as client:
            client.post(url, json=body)
    except Exception:
        pass


def warmup(transport: httpx.BaseTransport | None = None) -> None:
    """Warm both resident models; safe to run in a background thread."""
    try:
        if tier2_mode() == "off":
            return
        settings = get_settings()
        if settings.live_model:
            _ping(settings.live_model, live=True, transport=transport)
        if settings.judge_model:
            _ping(settings.judge_model, live=False, transport=transport)
    except Exception:
        pass
