"""Seeded canaries: fake secrets generated at runtime, never committed.

A canary looks like ``SF_CANARY_<KIND>_<16 hex>`` where the hex suffix is
``HMAC-SHA256(seed, kind)[:16]``. Tests build every canary at runtime from
a seed, so no canary value ever appears as a literal.
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import hmac
from urllib.parse import quote, unquote

PREFIX = "SF_CANARY_"


# Canary kinds the org plants; shared by the app, review and evaluation.
DEFAULT_KINDS = ("db_password", "api_key", "deploy_token", "smtp_password")


def make_canary(seed: str, kind: str) -> str:
    """Build the canary value for one kind from the seed."""
    digest = hmac.new(seed.encode("utf-8"), kind.encode("utf-8"),
                      hashlib.sha256).hexdigest()[:16]
    return f"{PREFIX}{kind.upper()}_{digest}"


class CanaryRegistry:
    """The set of canary values for one seed."""

    def __init__(self, seed: str, kinds: tuple[str, ...] | list[str]) -> None:
        self.seed = seed
        self.kinds = tuple(kinds)
        self.canaries: dict[str, str] = {
            kind: make_canary(seed, kind) for kind in self.kinds
        }

    def value(self, kind: str) -> str:
        """Return the canary value for one kind."""
        return self.canaries[kind]

    def values(self) -> list[str]:
        """Return every canary value in the registry."""
        return list(self.canaries.values())

    def __contains__(self, value: object) -> bool:
        return value in self.canaries.values()

    def kind_of(self, value: str) -> str | None:
        """Return the kind whose canary equals value, else None."""
        for kind, canary in self.canaries.items():
            if canary == value:
                return kind
        return None


def _rot13(value: str) -> str:
    return codecs.encode(value, "rot13")


def encodings(value: str) -> dict[str, str]:
    """Return the searchable transformed forms of one value."""
    raw = value.encode("utf-8")
    return {
        "exact": value,
        "base64": base64.b64encode(raw).decode("ascii"),
        "hex": raw.hex(),
        "rot13": _rot13(value),
        "reversed": value[::-1],
        "url": quote(value, safe=""),
    }


def decode_encoding(text: str, encoding: str) -> str | None:
    """Best-effort decode of one encoded blob; None when it does not decode."""
    try:
        if encoding == "base64":
            padded = text + "=" * (-len(text) % 4)
            return base64.b64decode(padded, validate=True).decode("utf-8")
        if encoding == "hex":
            return bytes.fromhex(text).decode("utf-8")
        if encoding == "rot13":
            return _rot13(text)
        if encoding == "reversed":
            return text[::-1]
        if encoding == "url":
            return unquote(text)
    except (ValueError, UnicodeDecodeError):
        return None
    return None
