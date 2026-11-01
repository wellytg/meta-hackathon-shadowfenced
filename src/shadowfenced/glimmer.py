"""Shared Ollama options for every local Glimmer call (T49, T53).

Every local Glimmer request -- judge verdicts, person-search intent, the
re-identification residual check, and the startup warm-up -- must use the
same ``options.num_ctx`` and ``keep_alive``. Ollama reloads the model
whenever a request asks for a different context size, so one stray
default (Ollama's built-in 8192) forces an ~18 s reload of the 17 GB
model on every size switch and trips the shared text-call timeout.
Text calls share one timeout (``SF_GLIMMER_TIMEOUT_S``, default 45 s);
image calls keep their own longer setting.
"""

from __future__ import annotations

import json as _json
import os as _os
import re as _re
from typing import Any

#: Fallback context size when no setting is available.
DEFAULT_GLIMMER_NUM_CTX = 2048
#: Fallback text-call timeout (seconds) when no setting is available.
#: Image calls keep their own longer setting (``SF_IMAGE_TIMEOUT_S``).
DEFAULT_GLIMMER_TIMEOUT_S = 45.0


def glimmer_num_ctx(settings: Any = None) -> int:
    """Shared ``num_ctx`` for every Glimmer call (never <= 0)."""
    if settings is None:
        from .config import get_settings

        try:
            settings = get_settings()
        except Exception:
            return DEFAULT_GLIMMER_NUM_CTX
    try:
        value = int(getattr(settings, "glimmer_num_ctx", DEFAULT_GLIMMER_NUM_CTX))
    except (TypeError, ValueError):
        return DEFAULT_GLIMMER_NUM_CTX
    return value if value > 0 else DEFAULT_GLIMMER_NUM_CTX


def glimmer_keep_alive() -> Any:
    """Shared ``keep_alive`` for every Glimmer call (model stays resident).

    ``SF_KEEP_ALIVE`` still overrides: eval runs set ``SF_KEEP_ALIVE=5m``
    so back-to-back judgements do not pin GPU memory, while production
    keeps the model resident with the default ``-1``.
    """
    raw = (_os.environ.get("SF_KEEP_ALIVE") or "-1").strip()
    if raw == "-1":
        return -1
    try:
        return int(raw)
    except ValueError:
        return raw


def glimmer_timeout_s(settings: Any = None) -> float:
    """Shared timeout (seconds) for every local Glimmer text call.

    Reads ``SF_GLIMMER_TIMEOUT_S`` via settings; falls back to
    ``DEFAULT_GLIMMER_TIMEOUT_S`` when missing, unparsable or <= 0.
    Image calls keep ``settings.image_timeout_s`` instead.
    """
    if settings is None:
        from .config import get_settings

        try:
            settings = get_settings()
        except Exception:
            return DEFAULT_GLIMMER_TIMEOUT_S
    try:
        value = float(getattr(settings, "glimmer_timeout_s",
                              DEFAULT_GLIMMER_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_GLIMMER_TIMEOUT_S
    return value if value > 0 else DEFAULT_GLIMMER_TIMEOUT_S


def glimmer_options(settings: Any = None, **extra: Any) -> dict[str, Any]:
    """Shared Ollama ``options`` for one Glimmer call.

    ``num_ctx`` always comes from the shared ``SF_GLIMMER_NUM_CTX``
    setting; per-call knobs (``temperature``, ``seed``, ``num_predict``,
    ``num_gpu``) pass through ``extra`` so prompts keep their own
    sampling behaviour.
    """
    options: dict[str, Any] = {"num_ctx": glimmer_num_ctx(settings)}
    options.update(extra)
    return options


#: Stray chat special tokens (``<|eot|>`` and kin) Glimmer sometimes
#: appends after otherwise-correct JSON. Stripped before parsing.
_SPECIAL_TOKEN_RE = _re.compile(r"<\|.*?\|>")


def parse_glimmer_json(content: object) -> Any:
    """Parse one Glimmer reply tolerantly.

    Strips ``<|...|>`` special tokens, then ``json.loads``; when that
    fails, retries on the outermost ``{...}`` slice so prose around the
    JSON still parses. Raises :class:`ValueError` when no JSON object
    can be recovered; callers map that to their own error type (or
    ``None`` when they fail soft).
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("glimmer returned no content")
    text = _SPECIAL_TOKEN_RE.sub("", content).strip()
    if not text:
        raise ValueError("glimmer returned non-JSON content")
    try:
        return _json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("glimmer returned non-JSON content") from None
        return _json.loads(text[start : end + 1])
