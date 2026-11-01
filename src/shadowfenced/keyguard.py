"""Protected API keys: recognise them in gated text without keeping them.

At startup (and on every gate, so settings overrides apply) the variable
names in ``SF_PROTECT_KEYS`` are read from the existing settings source
(dotenv plus environment, environment wins). For each name that currently
has a non-empty value, only an HMAC-SHA256 of the value under a random
per-process salt is retained -- never the value itself, and nothing here
is ever logged.

Detection itself re-reads the current values transiently for the gate
call and discards them before returning, so long-lived module state
holds only salted hashes, lengths, and per-variable hit counts. A
occurrence -- direct or in one of the encodings the canary scanner
already handles (base64, hex, reversed, url, split across whitespace) --
is reported as ``(variable_name, encoding)`` with the match offsets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from urllib.parse import quote

ENCODINGS = ("base64", "hex", "reversed", "url", "split")

# Random per-process salt: hashes cannot be compared across restarts and
# the raw values cannot be recovered from this module's state.
_SALT = secrets.token_bytes(32)

# Long-lived state holds hashes only: variable name -> HMAC-SHA256 hex.
_HASHES: dict[str, str] = {}
# Variable name -> value length (needed only for bookkeeping, not values).
_LENGTHS: dict[str, int] = {}
# Variable name -> number of blocked requests that carried it.
_COUNTS: dict[str, int] = {}
# Configured variable names from the last refresh (even value-less ones).
_NAMES: list[str] = []


def _configured_names() -> list[str]:
    """Variable names from the settings source (default META+MODEL keys)."""
    from .config import get_settings

    raw = get_settings().protect_keys or ""
    return [name.strip() for name in raw.split(",") if name.strip()]


def _current_values(names: list[str]) -> dict[str, str]:
    """Read the current raw values for names (dotenv + env, env wins).

    This is the only place values exist, as transient locals handed to
    the caller; callers must not retain or log them.
    """
    from .config import _find_dotenv, _parse_dotenv

    file_values: dict[str, str] = {}
    try:
        dotenv_path = _find_dotenv()
        if dotenv_path is not None:
            file_values = _parse_dotenv(dotenv_path)
    except Exception:
        file_values = {}
    merged = dict(file_values)
    merged.update(os.environ)
    return {name: merged.get(name, "") for name in names}


def _digest(value: str) -> str:
    """Salted HMAC-SHA256 hex of one value."""
    return hmac.new(_SALT, value.encode("utf-8"), hashlib.sha256).hexdigest()


def refresh() -> list[str]:
    """Re-read names/values; retain only salted hashes. Return the names."""
    names = _configured_names()
    values = _current_values(names)
    global _NAMES
    _NAMES = list(names)
    _HASHES.clear()
    _LENGTHS.clear()
    for name in names:
        value = values.get(name, "")
        if value:
            _HASHES[name] = _digest(value)
            _LENGTHS[name] = len(value)
        _COUNTS.setdefault(name, 0)
    for name in list(_COUNTS):
        if name not in names:
            del _COUNTS[name]
    return list(names)


def _encoded_forms(value: str) -> dict[str, str]:
    """The searchable transformed forms of one value (no rot13)."""
    raw = value.encode("utf-8")
    return {
        "exact": value,
        "base64": base64.b64encode(raw).decode("ascii"),
        "hex": raw.hex(),
        "reversed": value[::-1],
        "url": quote(value, safe=""),
    }


def _split_match(text: str, value: str) -> tuple[int, int] | None:
    """Match value chars with arbitrary whitespace between them."""
    if len(value) < 2:
        return None
    pattern = r"\s*".join(re.escape(ch) for ch in value)
    try:
        match = re.search(pattern, text)
    except re.error:
        return None
    if match is None:
        return None
    return match.start(), match.end()


def find_in_text(text: str) -> tuple[str, str, int, int] | None:
    """Find the first protected value encoded in text.

    Returns ``(variable_name, encoding, start, end)`` with code-point
    offsets into ``text``, or None. Refreshes the hash store first;
    values are transient and never retained or logged. Counts are not
    updated here -- call :func:`note_hit` once per blocked request.
    """
    names = refresh()
    values = _current_values(names)
    for name in names:
        value = values.get(name, "")
        if not value:
            continue
        for encoding, form in _encoded_forms(value).items():
            if not form:
                continue
            at = text.find(form)
            if at >= 0:
                return name, encoding, at, at + len(form)
        split = _split_match(text, value)
        if split is not None:
            return name, "split", split[0], split[1]
    return None


def note_hit(variable: str) -> None:
    """Count one blocked request carrying a protected variable."""
    _COUNTS[variable] = _COUNTS.get(variable, 0) + 1


def snapshot() -> list[dict[str, int | str]]:
    """Configured names with hit counts (no values, only hashes kept)."""
    names = refresh()
    return [
        {"name": name, "hits": _COUNTS.get(name, 0)} for name in names
    ]


def reset() -> None:
    """Forget hashes and counts (tests only)."""
    _HASHES.clear()
    _LENGTHS.clear()
    _COUNTS.clear()
    _NAMES.clear()
