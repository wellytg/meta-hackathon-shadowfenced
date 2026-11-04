"""Hosted demo mode (T25): a public demo without a GPU.

Turned on by ``SF_HOSTED=1``. The hosted demo:

- shows a fixed banner (startup line plus console and splash pages):
  ``Public demo. Use made-up data only.``;
- keeps custom rules in process memory only (never written to disk)
  and caps them at :data:`MAX_HOSTED_RULES`;
- caps uploads at :data:`HOSTED_MAX_FILE_BYTES`;
- rate-limits requests per client IP (a simple in-memory token bucket);
- judges Tier 2 with Muse Spark through the existing Meta upstream
  client when no local Ollama is reachable, with the same typed
  verdict schema and the same Tier 3 code verification.

Every hosted branch is guarded by :func:`is_hosted`, so local mode
behaviour is unchanged.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from .config import get_settings

HOSTED_BANNER = "Public demo. Use made-up data only."

MAX_HOSTED_RULES = 20

HOSTED_MAX_FILE_BYTES = 1 * 1024 * 1024

HOSTED_JUDGE_LABEL = "judge: Muse Spark (hosted demo)"

RATE_CAPACITY = 120.0
RATE_REFILL_PER_S = 2.0


def is_hosted() -> bool:
    """True when the public demo mode is on (``SF_HOSTED=1``)."""
    try:
        return bool(get_settings().hosted)
    except Exception:
        return False


# --- in-memory custom rules (hosted only; never touch disk) ---------------

_HOSTED_RULES: list[Any] = []
_HOSTED_LOCK = threading.Lock()


def hosted_rule_count() -> int:
    """Number of in-memory hosted custom rules."""
    with _HOSTED_LOCK:
        return len(_HOSTED_RULES)


def reset_hosted_rules() -> None:
    """Drop every in-memory hosted custom rule (tests only)."""
    with _HOSTED_LOCK:
        _HOSTED_RULES.clear()


# --- per-IP token bucket ----------------------------------------------------

_BUCKETS: dict[str, list[float]] = {}
_BUCKETS_LOCK = threading.Lock()


def allow_request(key: str, *, now: float | None = None) -> bool:
    """Take one token for ``key``; False when the bucket is empty."""
    moment = time.time() if now is None else now
    with _BUCKETS_LOCK:
        tokens, updated = _BUCKETS.get(key, [RATE_CAPACITY, moment])
        tokens = min(RATE_CAPACITY, tokens + (moment - updated) * RATE_REFILL_PER_S)
        if tokens < 1.0:
            _BUCKETS[key] = [tokens, moment]
            return False
        _BUCKETS[key] = [tokens - 1.0, moment]
        return True


def reset_rate_limiter() -> None:
    """Drop every rate-limit bucket (tests only)."""
    with _BUCKETS_LOCK:
        _BUCKETS.clear()


# --- Tier 2 judge selection --------------------------------------------------


def ollama_reachable(
    *, transport: httpx.BaseTransport | None = None, timeout: float = 2.0
) -> bool:
    """True when a local Ollama answers; False on any failure."""
    try:
        url = get_settings().ollama_url.rstrip("/") + "/api/version"
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.get(url)
        return response.status_code == 200
    except Exception:
        return False


def meta_judge(
    window: str,
    *,
    transport: httpx.BaseTransport | None = None,
    settings: Any = None,
) -> Any:
    """Judge one window with Muse Spark via the Meta upstream client.

    Uses the same typed verdict schema as the local judge
    (:class:`judge.Judgement`); None on any failure so the review
    fails closed. Returns None when Meta is unavailable.
    """
    from . import judge as judge_module
    from . import upstream as upstream_module

    cfg = settings or get_settings()
    if not cfg.meta_available:
        return None
    messages = [
        {
            "role": "system",
            "content": judge_module.SYSTEM_PROMPT + HOSTED_JUDGE_SCHEMA,
        },
        {"role": "user", "content": judge_module.quote_as_data(window)},
    ]
    try:
        answer = upstream_module.chat_sync(
            messages, provider="meta", settings=cfg, transport=transport
        )
    except Exception:
        return None
    return _parse_meta_judgement(answer)


#: The local judge gets the verdict schema through Ollama's ``format``
#: option; the Meta API call does not, so the keys and classes are
#: spelled out here (T77). Without them every hosted review failed
#: closed as "unavailable".
HOSTED_JUDGE_SCHEMA = (
    " Reply with one JSON object and nothing else, with exactly these keys:"
    ' "cls" (one of "secret", "pii", "business", "beacon", "benign"),'
    ' "is_beacon" (true or false), "sensitivity" (number 0 to 1),'
    ' "confidence" (number 0 to 1), "span" (string).'
)


def _parse_meta_judgement(answer: Any) -> Any:
    """Parse Spark's verdict: plain JSON, fenced JSON, or JSON in text.

    Same strict schema as the local judge; None on anything else, so
    the review still fails closed.
    """
    from . import judge as judge_module

    if not isinstance(answer, str) or not answer.strip():
        return None
    direct = judge_module._parse_content(answer)
    if direct is not None:
        return direct
    start, end = answer.find("{"), answer.rfind("}")
    if start == -1 or end <= start:
        return None
    return judge_module._parse_content(answer[start : end + 1])


def meta_person_search(
    window: str,
    *,
    transport: httpx.BaseTransport | None = None,
    settings: Any = None,
) -> Any:
    """Judge person-search intent with Muse Spark via the Meta client.

    Uses the same strict typed schema as the local Glimmer check
    (:class:`person_search.PersonSearchJudgement`); labels only.
    None on any failure so the caller falls back to the code path.
    Returns None when Meta is unavailable.
    """
    from . import person_search as person_search_module
    from . import upstream as upstream_module

    cfg = settings or get_settings()
    if not cfg.meta_available:
        return None
    messages = [
        {
            "role": "system",
            "content": person_search_module.PERSON_SEARCH_SYSTEM
            + " Reply with JSON only.",
        },
        {
            "role": "user",
            "content": window[-person_search_module.PERSON_SEARCH_TEXT_CHARS:],
        },
    ]
    try:
        answer = upstream_module.chat_sync(
            messages, provider="meta", settings=cfg, transport=transport
        )
    except Exception:
        return None
    if not isinstance(answer, str) or not answer.strip():
        return None
    import json as _json

    cleaned = answer.strip()
    try:
        raw = _json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            raw = _json.loads(cleaned[start : end + 1])
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        validated = person_search_module.PersonSearchJudgement.model_validate(raw)
    except Exception:
        return None
    return validated.model_dump()


def judge_for_review(
    window: str,
    model: str,
    *,
    judge_transport: httpx.BaseTransport | None = None,
    meta_transport: httpx.BaseTransport | None = None,
    reachable: bool | None = None,
) -> tuple[Any, str | None]:
    """Judge one review window; return ``(judgement, trace_label)``.

    Local mode is unchanged (Ollama judge, no label). In hosted mode
    with no local Ollama reachable, the Meta judge answers instead and
    the trace label names it. ``SF_TIER2_MODE=off`` disables judging
    everywhere. ``reachable`` and the transports are injectable so
    tests never touch the network.
    """
    from . import judge as judge_module

    if get_settings().tier2_mode == "off":
        return None, None
    if not is_hosted():
        return (
            judge_module.judge(
                window, model, live=False, transport=judge_transport
            ),
            None,
        )
    if reachable is None:
        reachable = ollama_reachable()
    if reachable:
        return (
            judge_module.judge(
                window, model, live=False, transport=judge_transport
            ),
            None,
        )
    return meta_judge(window, transport=meta_transport), HOSTED_JUDGE_LABEL
