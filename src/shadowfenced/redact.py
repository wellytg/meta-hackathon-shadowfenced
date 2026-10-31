"""Redaction: per-class placeholders plus tolerant re-hydration.

Placeholders are numbered per base label: ``[SECRET_1]``, ``[PII_EMAIL_1]``,
``[PII_PHONE_1]``, ``[CODENAME_1]``, ``[CLIENT_1]``, ``[FIGURE_1]``,
``[BEACON_1]``. The same value always maps to the same placeholder within
one redaction. The placeholder map lives server-side only, in memory, with
a 15 minute TTL, and is never logged.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .spans import Span

# Tolerant placeholder shape: case, spacing, "_" and "-" all accepted.
# Extends the plan regex with FIGURE (a listed placeholder with no other
# spelling), matched case-insensitively.
PLACEHOLDER_RE = re.compile(
    r"\[\s*(SECRET|PII_[A-Z]+|CODENAME|CLIENT|FIGURE|BEACON)[\s_-]*(\d+)\s*\]",
    re.IGNORECASE,
)

STORE_TTL_S = 15 * 60

# decision_id -> (mapping, expires_at_epoch_s)
_STORE: dict[str, tuple[dict[str, str], float]] = {}


def base_label(cls: str, subclass: str) -> str:
    """Placeholder base label for one span class/subclass."""
    if cls == "secret":
        return "SECRET"
    if cls == "pii":
        return "PII_" + re.sub(r"[^A-Z0-9]+", "_", subclass.upper()).strip("_")
    if cls == "business":
        return re.sub(r"[^A-Z0-9]+", "_", subclass.upper()).strip("_")
    if cls == "beacon":
        return "BEACON"
    return re.sub(r"[^A-Z0-9]+", "_", cls.upper()).strip("_")


def _canonical(label: str, number: str) -> str:
    """Normalise one tolerant placeholder match to ``[LABEL_N]`` form."""
    norm = re.sub(r"[\s_-]+", "_", label.strip().upper())
    return f"[{norm}_{int(number)}]"


def redact(text: str, spans: list[Span]) -> tuple[str, dict[str, str]]:
    """Replace every span with its placeholder.

    Returns ``(redacted_text, mapping)`` where mapping is
    ``{placeholder: value}`` with bracketed canonical placeholders.
    The same (label, value) pair reuses its placeholder; numbering
    restarts per base label. Spans are code-point ranges.
    """
    counters: dict[str, int] = {}
    reverse: dict[tuple[str, str], str] = {}
    mapping: dict[str, str] = {}
    edits: list[tuple[int, int, str]] = []
    for span in sorted(spans, key=lambda s: (s.cp_start, s.cp_end)):
        if span.cp_start < 0 or span.cp_end > len(text):
            continue
        if span.cp_start >= span.cp_end:
            continue
        if edits and span.cp_start < edits[-1][1]:
            # Overlaps an accepted edit; callers pass merged spans,
            # so this only guards unmerged input.
            continue
        value = text[span.cp_start:span.cp_end]
        label = base_label(span.cls, span.subclass)
        key = (label, value)
        placeholder = reverse.get(key)
        if placeholder is None:
            counters[label] = counters.get(label, 0) + 1
            placeholder = f"[{label}_{counters[label]}]"
            reverse[key] = placeholder
            mapping[placeholder] = value
        edits.append((span.cp_start, span.cp_end, placeholder))
    out = text
    for start, end, placeholder in reversed(edits):
        out = out[:start] + placeholder + out[end:]
    return out, mapping


def rehydrate(
    answer: str, mapping: dict[str, str]
) -> tuple[str, list[dict], list[str]]:
    """Swap placeholders in a model answer back to their values.

    Matching is tolerant (case/spacing/``_``/``-``). Returns
    ``(text, highlights, unmatched)`` where highlights are
    ``{start, end, placeholder}`` code-point offsets into the returned
    text and unmatched lists canonical placeholders with no mapping.
    """
    if not mapping and PLACEHOLDER_RE.search(answer) is None:
        return answer, [], []
    lookup = {_canonical(*_split_key(k)): v for k, v in mapping.items()}
    highlights: list[dict] = []
    unmatched: list[str] = []
    parts: list[str] = []
    cursor = 0
    out_len = 0
    for match in PLACEHOLDER_RE.finditer(answer):
        canonical = _canonical(match.group(1), match.group(2))
        parts.append(answer[cursor:match.start()])
        out_len += match.start() - cursor
        value = lookup.get(canonical)
        if value is None:
            parts.append(match.group(0))
            out_len += match.end() - match.start()
            if canonical not in unmatched:
                unmatched.append(canonical)
        else:
            parts.append(value)
            highlights.append(
                {
                    "start": out_len,
                    "end": out_len + len(value),
                    "placeholder": canonical,
                }
            )
            out_len += len(value)
        cursor = match.end()
    parts.append(answer[cursor:])
    return "".join(parts), highlights, unmatched


def _split_key(key: str) -> tuple[str, str]:
    """Split a canonical ``[LABEL_N]`` key into (label, number)."""
    inner = key.strip().strip("[]")
    label, _, number = inner.rpartition("_")
    if not label or not number.strip().isdigit():
        return inner, "0"
    return label, number.strip()


def save_mapping(decision_id: str, mapping: dict[str, str]) -> None:
    """Store one decision's placeholder map with a 15 minute TTL."""
    _STORE[decision_id] = (dict(mapping), time.time() + STORE_TTL_S)


def load_mapping(decision_id: str) -> dict[str, str]:
    """Return one decision's placeholder map, or {} when missing/expired."""
    entry = _STORE.get(decision_id)
    if entry is None:
        return {}
    mapping, expires_at = entry
    if time.time() > expires_at:
        del _STORE[decision_id]
        return {}
    return dict(mapping)


def drop_mapping(decision_id: str) -> None:
    """Forget one decision's placeholder map immediately."""
    _STORE.pop(decision_id, None)
