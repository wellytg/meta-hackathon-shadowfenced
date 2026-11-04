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
    r"\[\s*(SECRET|PII_[A-Z]+(?:_[A-Z]+)*|CODENAME|CLIENT|FIGURE|BEACON|CONTRACT_CLAUSE)[\s_-]*(\d+)\s*\]",
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


def _bare_pattern(lookup: dict[str, str]) -> re.Pattern[str] | None:
    """Match known placeholder names written without brackets.

    Keys are canonical ``[LABEL_N]`` placeholders; the pattern matches
    the bare ``LABEL_N`` form with word boundaries, longest first so
    ``CODENAME_1`` never matches inside ``CODENAME_12``. Matching is
    case-insensitive, mirroring the bracketed form.
    """
    names = sorted(
        {key[1:-1].upper() for key in lookup},
        key=len,
        reverse=True,
    )
    if not names:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b",
                      re.IGNORECASE)


#: Markdown inline link/image targets: only the ``(target)`` part is a
#: no-restore zone; the display text restores normally.
_LINK_TARGET_RE = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]*)\)")

#: Bare URLs (any scheme, plus mailto:) up to whitespace or a quote or
#: angle bracket. Deliberately over-wide: covering a neighbouring char
#: only keeps one more placeholder redacted, while missing a URL would
#: leak a value into a link the user might open.
_BARE_URL_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*://|mailto:)[^\s<>\"']+")


def _url_skip_spans(answer: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """No-restore zones: markdown link/image targets, then bare URLs (T81)."""
    targets = [
        (match.start(1), match.end(1))
        for match in _LINK_TARGET_RE.finditer(answer)
    ]
    urls = [
        (match.start(), match.end())
        for match in _BARE_URL_RE.finditer(answer)
    ]
    return targets, urls


def _skip_kind_at(
    pos: int,
    targets: list[tuple[int, int]],
    urls: list[tuple[int, int]],
) -> str | None:
    """Why position ``pos`` keeps its placeholder, if it does (T81)."""
    for start, end in targets:
        if start <= pos < end:
            return "link target"
    for start, end in urls:
        if start <= pos < end:
            return "URL"
    return None


def _restore_bare(
    segment: str, lookup: dict[str, str], pattern: re.Pattern[str],
    out_len: int, base: int = 0,
    targets: list[tuple[int, int]] | None = None,
    urls: list[tuple[int, int]] | None = None,
) -> tuple[str, list[dict], int]:
    """Restore bare placeholder names inside one bracket-free segment.

    ``base`` is the segment's offset in the full answer, so URL zones
    (computed over the whole answer) still line up.
    """
    pieces: list[str] = []
    highlights: list[dict] = []
    cursor = 0
    for match in pattern.finditer(segment):
        canonical = f"[{match.group(1).upper()}]"
        value = lookup.get(canonical)
        if value is None:
            continue
        kind = _skip_kind_at(
            base + match.start(), targets or [], urls or []
        )
        if kind is not None:
            pieces.append(segment[cursor:match.end()])
            start = out_len + match.start() - cursor
            length = match.end() - match.start()
            highlights.append(
                {"start": start, "end": start + length,
                 "placeholder": canonical,
                 "note": f"left redacted inside a {kind}"}
            )
            out_len += match.end() - cursor
            cursor = match.end()
            continue
        pieces.append(segment[cursor:match.start()])
        start = out_len + match.start() - cursor
        pieces.append(value)
        highlights.append(
            {"start": start, "end": start + len(value),
             "placeholder": canonical}
        )
        out_len += match.end() - cursor - len(match.group(0)) + len(value)
        cursor = match.end()
    pieces.append(segment[cursor:])
    out_len += len(segment) - cursor
    return "".join(pieces), highlights, out_len


def rehydrate(
    answer: str, mapping: dict[str, str]
) -> tuple[str, list[dict], list[str]]:
    """Swap placeholders in a model answer back to their values.

    Matching is tolerant (case/spacing/``_``/``-``). A known
    placeholder name written without brackets (``CODENAME_1``) is also
    restored when it exactly matches a mapped name with word
    boundaries. Returns ``(text, highlights, unmatched)`` where
    highlights are ``{start, end, placeholder}`` code-point offsets
    into the returned text and unmatched lists canonical placeholders
    with no mapping. Bare names with no mapping are plain text: they
    are left alone and never reported as unmatched.

    A placeholder sitting inside a URL or a markdown link/image target
    is never restored (the value would leak into a link the user might
    open): the placeholder stays in place and the highlights carry a
    ``note`` entry pointing at it.
    """
    if not mapping and PLACEHOLDER_RE.search(answer) is None:
        return answer, [], []
    lookup = {_canonical(*_split_key(k)): v for k, v in mapping.items()}
    bare = _bare_pattern(lookup)
    targets, urls = _url_skip_spans(answer)
    highlights: list[dict] = []
    unmatched: list[str] = []
    parts: list[str] = []
    cursor = 0
    out_len = 0
    for match in PLACEHOLDER_RE.finditer(answer):
        canonical = _canonical(match.group(1), match.group(2))
        gap = answer[cursor:match.start()]
        if bare is not None and gap:
            restored, gap_marks, out_len = _restore_bare(
                gap, lookup, bare, out_len, cursor, targets, urls
            )
            parts.append(restored)
            highlights.extend(gap_marks)
        else:
            parts.append(gap)
            out_len += len(gap)
        value = lookup.get(canonical)
        if value is None:
            parts.append(match.group(0))
            out_len += match.end() - match.start()
            if canonical not in unmatched:
                unmatched.append(canonical)
        else:
            kind = _skip_kind_at(match.start(), targets, urls)
            if kind is not None:
                parts.append(match.group(0))
                highlights.append(
                    {
                        "start": out_len,
                        "end": out_len + match.end() - match.start(),
                        "placeholder": canonical,
                        "note": f"left redacted inside a {kind}",
                    }
                )
                out_len += match.end() - match.start()
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
    tail = answer[cursor:]
    if bare is not None and tail:
        restored, tail_marks, out_len = _restore_bare(
            tail, lookup, bare, out_len, cursor, targets, urls
        )
        parts.append(restored)
        highlights.extend(tail_marks)
    else:
        parts.append(tail)
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
