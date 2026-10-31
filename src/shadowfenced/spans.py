"""Tier 1 spans: code-proven findings with UTF-16 offsets.

Offsets follow the API contract: ``start``/``end`` count UTF-16 code units
(so an emoji counts 2), while ``cp_start``/``cp_end`` count Unicode code
points (Python string indices).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

CLASSES = ("secret", "pii", "business", "beacon")
STATUSES = ("proven", "ambiguous")
ACTIONS = ("redact", "block", "review")

# Severity rank used when overlapping spans merge: the more severe wins.
# Mirrors T4 ordering BLOCK > HOLD(review) > REDACT.
_ACTION_RANK = {"block": 3, "review": 2, "redact": 1}
_CLASS_RANK = {"secret": 4, "pii": 3, "business": 2, "beacon": 1}


def cp_to_utf16(text: str, cp_index: int) -> int:
    """Convert a code-point index into a UTF-16 code-unit offset."""
    total = 0
    for ch in text[:cp_index]:
        total += 2 if ord(ch) > 0xFFFF else 1
    return total


def utf16_to_cp(text: str, utf16_index: int) -> int:
    """Convert a UTF-16 code-unit offset into a code-point index."""
    used = 0
    for i, ch in enumerate(text):
        width = 2 if ord(ch) > 0xFFFF else 1
        if used + width > utf16_index:
            return i
        used += width
    return len(text)


def make_span_id(cls: str, cp_start: int, cp_end: int) -> str:
    """Short hash of the span class plus its code-point range."""
    digest = hashlib.sha256(f"{cls}:{cp_start}:{cp_end}".encode()).hexdigest()
    return digest[:12]


@dataclass
class Span:
    """One Tier 1 finding."""

    start: int  # UTF-16 code units
    end: int  # UTF-16 code units
    cp_start: int  # code points
    cp_end: int  # code points
    cls: str  # secret | pii | business | beacon
    subclass: str
    tier: int = 1
    proof: str = "code"
    status: str = "proven"  # proven | ambiguous
    message: str = ""
    why: str = ""
    suggested_action: str = "redact"  # redact | block | review
    can_keep: bool = False
    confidence: float = 0.9
    span_id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.span_id:
            self.span_id = make_span_id(self.cls, self.cp_start, self.cp_end)

    def severity(self) -> tuple[int, int]:
        """Higher sorts as more severe (action first, then class)."""
        return (_ACTION_RANK.get(self.suggested_action, 0),
                _CLASS_RANK.get(self.cls, 0))


def span_from_cp_range(text: str, cp_start: int, cp_end: int, **kwargs) -> Span:
    """Build a Span from code-point offsets, deriving UTF-16 offsets + id."""
    return Span(
        start=cp_to_utf16(text, cp_start),
        end=cp_to_utf16(text, cp_end),
        cp_start=cp_start,
        cp_end=cp_end,
        **kwargs,
    )


def _merge_winner(current: Span, span: Span) -> Span:
    """Pick the span whose metadata survives a merge.

    A ``canary`` subclass always wins over another span of the same class:
    it decides BLOCK, so a wider password-assignment span must never hide
    it. Otherwise the more severe span wins (ties keep the earlier one).
    """
    if current.cls == span.cls:
        if current.subclass == "canary":
            return current
        if span.subclass == "canary":
            return span
    return span if span.severity() > current.severity() else current


def merge_overlaps(spans: list[Span]) -> list[Span]:
    """Merge spans whose code-point ranges overlap, keeping the more severe.

    The merged span covers the union of the ranges but carries the metadata
    (class, action, message, ...) of the more severe input span.
    """
    ordered = sorted(spans, key=lambda s: (s.cp_start, s.cp_end))
    merged: list[Span] = []
    for span in ordered:
        if merged and span.cp_start < merged[-1].cp_end:
            current = merged[-1]
            winner = _merge_winner(current, span)
            merged[-1] = Span(
                # UTF-16 offsets are monotonic in cp offsets, so the union's
                # bounds are the min start and max end of the inputs.
                start=min(current.start, span.start),
                end=max(current.end, span.end),
                cp_start=min(current.cp_start, span.cp_start),
                cp_end=max(current.cp_end, span.cp_end),
                cls=winner.cls,
                subclass=winner.subclass,
                tier=winner.tier,
                proof=winner.proof,
                status=winner.status,
                message=winner.message,
                why=winner.why,
                suggested_action=winner.suggested_action,
                can_keep=winner.can_keep,
                confidence=winner.confidence,
            )
        else:
            merged.append(span)
    return merged
