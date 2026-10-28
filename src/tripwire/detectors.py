"""The deterministic canary detector.

Matches seeded canary secrets against outbound text exactly and in
transformed forms: base64, hex, rot13, reversed, split across fields, plus
high-entropy substring flagging. Pure functions over strings: no network,
no model, no I/O. Runs before any model call, and a hit here can never be
overridden by the watcher.
"""

from __future__ import annotations

from dataclasses import dataclass


ENCODINGS: tuple[str, ...] = ("exact", "base64", "hex", "rot13", "reversed", "split_fields")


@dataclass
class DetectorHit:
    """One canary match in outbound text."""

    canary_id: str
    encoding: str
    field: str
    span_start: int
    span_end: int


def encode_canary(canary: str, encoding: str) -> list[str]:
    """Return the searchable forms of a canary for one encoding."""
    raise NotImplementedError


def scan_text(text: str, canaries: dict[str, str], field: str) -> list[DetectorHit]:
    """Scan one text field for all canaries in all encodings."""
    raise NotImplementedError


def scan_action(arguments: dict[str, str], canaries: dict[str, str]) -> list[DetectorHit]:
    """Scan every string field of a tool call's arguments."""
    raise NotImplementedError


def entropy(text: str) -> float:
    """Shannon entropy of a string, for high-entropy substring flagging."""
    raise NotImplementedError
