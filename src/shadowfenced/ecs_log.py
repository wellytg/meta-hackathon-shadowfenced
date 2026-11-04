"""Hash-chained JSONL decision log (T7, T69 habits).

Every gate decision is appended as one JSON object per line to the file at
``SF_LOG_PATH`` (default ``runs/decisions.jsonl``). Each decision event
carries::

    {event, ts, decision_id, decision, tier, classes, kinds, rule_ids,
     cue, site, source, fingerprints, domain_prints,
     latency_ms, prev_hash, hash}

``fingerprints`` are ``HMAC-SHA256(secret, value)[:12]`` under a
per-install secret (32 random bytes, created on first use); raw values
are never written, and low-entropy values cannot be brute-forced back
without the secret. Old events keep their unsalted fingerprints and
:func:`verify` still passes: the hash chain never covers fingerprint
provenance.

Habit events share the same chain (fields only, never values):

- ``choice``: the person acted on a card (send anyway, generalise,
  cancel, redacted copy, upload anyway, approve/deny, apply/dismiss a
  suggestion).
- ``rule_change``: a rule suggestion was applied or undone.
- ``history_cleared``: the person deleted their history; the log is
  truncated to this marker, which stays chained to the deleted tail.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Decision

DEFAULT_LOG_PATH = "runs/decisions.jsonl"
GENESIS_HASH = "0" * 64

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_LAST_WRITE_ERROR: str | None = None


def get_last_write_error() -> str | None:
    """Return the last decision-log write failure type, if any."""
    return _LAST_WRITE_ERROR


def clear_last_write_error() -> None:
    """Forget any recorded decision-log write failure (tests only)."""
    global _LAST_WRITE_ERROR
    _LAST_WRITE_ERROR = None


def record_write_error(exc: BaseException) -> str:
    """Remember a write failure type for the ``log`` health line."""
    global _LAST_WRITE_ERROR
    name = type(exc).__name__
    _LAST_WRITE_ERROR = name
    return name


def note_write_failure(exc: BaseException, path: str | Path | None) -> str:
    """Record a write failure and log one warning (never message text)."""
    name = record_write_error(exc)
    logging.getLogger(__name__).warning(
        "decision log not writable: %s path=%s", name, str(path)
    )
    return name


def _resolve_one(value: str | Path) -> str:
    text = str(value)
    if not text.strip():
        text = DEFAULT_LOG_PATH
    candidate = Path(text)
    if candidate.is_absolute():
        return str(candidate)
    return str(_PROJECT_ROOT / candidate)


def resolve_path(explicit: str | Path | None = None) -> str:
    """Return the log path: explicit arg, ``SF_LOG_PATH``, or the default.

    Relative paths resolve against the project root (the same root
    ``policy.py`` uses), not the process working directory, so the
    hosted container finds the log no matter where it starts. Absolute
    paths (including a ``SF_LOG_PATH`` absolute override) pass through.
    """
    if explicit:
        return _resolve_one(explicit)
    return _resolve_one(os.environ.get("SF_LOG_PATH", DEFAULT_LOG_PATH))


# --- per-install fingerprint secret --------------------------------------

#: Keyring account holding the hex-encoded fingerprint secret.
SECRET_KEYRING_ACCOUNT = "log-fingerprint-secret"

#: Default file holding the hex-encoded secret when no keyring exists.
#: Outside the repo, with user-only permissions.
DEFAULT_SECRET_PATH = Path.home() / ".shadowfenced" / "fingerprint_secret"

_SECRET: bytes | None = None


def reset_fingerprint_secret() -> None:
    """Forget the cached fingerprint secret (tests only)."""
    global _SECRET
    _SECRET = None


def secret_path(settings: Any | None = None) -> Path:
    """Where the fingerprint secret file lives.

    ``SF_FINGERPRINT_SECRET_FILE`` wins, else
    ``~/.shadowfenced/fingerprint_secret``. The ``SF_FINGERPRINT_SECRET``
    env var bypasses the file entirely when set (tests and containers).
    """
    override = ""
    if settings is not None:
        override = str(getattr(settings, "fingerprint_secret_file", "") or "").strip()
    if not override:
        override = os.environ.get("SF_FINGERPRINT_SECRET_FILE", "").strip()
    if override:
        return Path(override)
    return DEFAULT_SECRET_PATH


def _secret_from_env(settings: Any | None = None) -> bytes | None:
    """Explicit secret from env/settings (None when unset).

    A 64-char hex value is decoded; any other non-empty value is used
    as UTF-8 bytes. The value itself is never logged.
    """
    raw = ""
    if settings is not None:
        raw = str(getattr(settings, "fingerprint_secret", "") or "")
    if not raw:
        raw = os.environ.get("SF_FINGERPRINT_SECRET", "")
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return bytes.fromhex(text)
    return text.encode("utf-8")


def _secret_from_keyring() -> bytes | None:
    """Read the hex secret from the OS credential store (None if none)."""
    try:
        from . import providers as providers_module

        if not providers_module.keyring_available():
            return None
        import keyring  # type: ignore[import-not-found]

        stored = keyring.get_password(
            providers_module.SERVICE_NAME, SECRET_KEYRING_ACCOUNT
        )
    except Exception:
        return None
    if not stored or not stored.strip():
        return None
    try:
        return bytes.fromhex(stored.strip())
    except ValueError:
        return None


def _secret_to_keyring(secret: bytes) -> bool:
    """Persist the hex secret in the OS credential store."""
    try:
        from . import providers as providers_module

        import keyring  # type: ignore[import-not-found]

        keyring.set_password(
            providers_module.SERVICE_NAME,
            SECRET_KEYRING_ACCOUNT,
            secret.hex(),
        )
        return True
    except Exception:
        return False


def load_or_create_secret(settings: Any | None = None) -> bytes:
    """Return the per-install fingerprint secret (32 random bytes).

    Precedence: ``SF_FINGERPRINT_SECRET`` env wins and is never
    written anywhere; otherwise the OS credential store (via keyring)
    when available; otherwise a local file outside the repo with
    user-only permissions, created on first use. The secret value is
    never logged and never sent anywhere.
    """
    global _SECRET
    if _SECRET is not None:
        return _SECRET
    if settings is None:
        try:
            from .config import get_settings

            settings = get_settings()
        except Exception:
            settings = None
    explicit = _secret_from_env(settings)
    if explicit is not None:
        _SECRET = explicit
        return _SECRET
    stored = _secret_from_keyring()
    if stored is not None:
        _SECRET = stored
        return _SECRET
    path = secret_path(settings)
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        try:
            _SECRET = bytes.fromhex(existing.split()[0])
            return _SECRET
        except ValueError:
            pass
    import secrets as _secrets

    fresh = _secrets.token_bytes(32)
    if _secret_to_keyring(fresh):
        _SECRET = fresh
        return _SECRET
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fresh.hex() + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    _SECRET = fresh
    return _SECRET


def fingerprint(value: str) -> str:
    """Short HMAC fingerprint of one raw value; the value never logs."""
    secret = load_or_create_secret()
    return hmac.new(
        secret, value.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:12]


def _span_value(text: str, cp_start: int, cp_end: int) -> str:
    """Slice one span's raw value out of the gated text (code points)."""
    if 0 <= cp_start < cp_end <= len(text):
        return text[cp_start:cp_end]
    return ""


# --- field sanitisers (fields only; values can never reach the log) ------

#: Fixed choice vocabulary for card actions.
CHOICES = frozenset(
    {
        "send_anyway",
        "send_generalised",
        "generalise",
        "cancel",
        "redacted_copy",
        "upload_anyway",
        "approve",
        "deny",
        "apply_suggestion",
        "dismiss_suggestion",
    }
)

_KIND_RE = re.compile(r"[a-z0-9_]{1,32}:[a-z0-9_]{1,48}")
_KIND_CLASSES = frozenset({"secret", "pii", "business", "beacon"})
_RULE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_DECISION_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_SITE_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


def clean_site(value: Any) -> str:
    """Reduce a site to its host only (lowercase, no port/path)."""
    text = str(value or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if text.startswith("[") and "]" in text:
        text = text[1:].split("]", 1)[0]
    else:
        text = text.split(":", 1)[0]
    text = text.strip().rstrip(".")
    if text in ("", "localhost"):
        return "app" if text == "" else "localhost"
    if text == "app":
        return "app"
    if _SITE_RE.fullmatch(text):
        return text
    return "app"


def clean_choice(value: Any) -> str:
    """Validate one choice against the fixed vocabulary."""
    text = str(value or "").strip().lower()
    if text in CHOICES:
        return text
    raise ValueError(f"unknown choice {str(value)[:32]!r}")


def clean_kinds(values: Any) -> list[str]:
    """Validate ``cls:subclass`` kind labels (fixed shape, no values)."""
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ValueError("kinds must be a list")
    out: list[str] = []
    for item in list(values)[:32]:
        text = str(item or "").strip().lower()
        if not _KIND_RE.fullmatch(text):
            raise ValueError(f"bad kind {str(item)[:32]!r}")
        cls = text.split(":", 1)[0]
        if cls not in _KIND_CLASSES:
            raise ValueError(f"bad kind class {str(item)[:32]!r}")
        if text not in out:
            out.append(text)
    return out


def clean_rule_ids(values: Any) -> list[str]:
    """Keep known catalogue rule ids; drop anything else."""
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        return []
    try:
        from . import rule_catalog as catalog_module

        known = set(getattr(catalog_module, "_CATALOGUE_ORDER", ()))
    except Exception:
        known = set()
    out: list[str] = []
    for item in list(values)[:32]:
        text = str(item or "").strip()
        if not _RULE_ID_RE.fullmatch(text):
            continue
        if known and text not in known:
            continue
        if text not in out:
            out.append(text)
    return out


def clean_decision_id(value: Any) -> str:
    """Validate one decision/file id (opaque token, capped length)."""
    text = str(value or "").strip()
    if not _DECISION_ID_RE.fullmatch(text):
        raise ValueError("bad decision_id")
    return text


def clean_source(value: Any) -> str:
    """Validate the source marker (``text`` or ``file``)."""
    text = str(value or "").strip().lower()
    return "file" if text == "file" else "text"


def span_kind(span: Any) -> str:
    """Kind label for one span: ``cls:subclass`` (fixed vocabulary)."""
    cls = re.sub(r"[^a-z0-9_]", "", str(getattr(span, "cls", "") or "").lower())
    sub = re.sub(
        r"[^a-z0-9_]", "", str(getattr(span, "subclass", "") or "").lower()
    )
    if cls not in _KIND_CLASSES or not sub:
        return ""
    return f"{cls}:{sub[:48]}"


def _span_kinds(spans: list[Any]) -> list[str]:
    kinds: list[str] = []
    for span in spans:
        kind = span_kind(span)
        if kind and kind not in kinds:
            kinds.append(kind)
    return sorted(kinds)


def _email_domain(value: str) -> str:
    """Domain part of one email value (lowercased, "" when not email-like)."""
    text = value.strip().lower()
    if "@" not in text:
        return ""
    domain = text.rsplit("@", 1)[1].strip().rstrip(".")
    if "." not in domain or not _SITE_RE.fullmatch(domain):
        return ""
    return domain


def _domain_prints(text: str, spans: list[Any]) -> list[str]:
    """HMAC fingerprints of email domains only (never the addresses)."""
    prints: list[str] = []
    for span in spans:
        if str(getattr(span, "cls", "") or "") != "pii":
            continue
        if str(getattr(span, "subclass", "") or "") != "email":
            continue
        domain = _email_domain(
            _span_value(
                text,
                int(getattr(span, "cp_start", -1) or -1),
                int(getattr(span, "cp_end", -1) or -1),
            )
        )
        if domain:
            printed = fingerprint("domain:" + domain)
            if printed not in prints:
                prints.append(printed)
    return sorted(prints)


def _cue_of(text: str) -> str | None:
    """Sensitivity cue word in the text, if any (fixed vocabulary)."""
    try:
        from .engine import cue_word
    except Exception:
        return None
    try:
        found = cue_word(text)
    except Exception:
        return None
    if not isinstance(found, str) or not found:
        return None
    return found[:64]


def _hash_record(record: dict[str, Any]) -> str:
    """Hash one record (without its ``hash`` field) chained to ``prev_hash``."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        (record["prev_hash"] + canonical).encode("utf-8")
    ).hexdigest()


def _last_hash(path: Path) -> tuple[str, int]:
    """Return (last hash, event count) of an existing log file."""
    prev = GENESIS_HASH
    count = 0
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                prev = json.loads(line)["hash"]
                count += 1
    return prev, count


def _append_record(log_path: str | None, record: dict[str, Any]) -> dict[str, Any]:
    """Chain and append one event record; return the stored record."""
    path = Path(resolve_path(log_path))
    if path.parent != Path("") and str(path.parent):
        path.parent.mkdir(parents=True, exist_ok=True)
    prev_hash, _ = _last_hash(path)
    record["prev_hash"] = prev_hash
    record["hash"] = _hash_record(record)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def append_decision(
    log_path: str | None,
    decision: Decision,
    text: str = "",
    *,
    site: str = "app",
    source: str = "text",
) -> dict[str, Any]:
    """Append one gate decision; return the stored record.

    ``text`` is the gated user text, used only to fingerprint span
    values and spot the (fixed-vocabulary) sensitivity cue — it is
    never written to the log.
    """
    classes = sorted({span.cls for span in decision.spans})
    prints = [
        fingerprint(_span_value(text, span.cp_start, span.cp_end))
        for span in decision.spans
    ]
    latency = decision.latency_ms
    record: dict[str, Any] = {
        "event": "decision",
        "ts": datetime.now(timezone.utc).isoformat(),
        "decision_id": decision.decision_id,
        "decision": decision.decision,
        "tier": decision.resolved_tier,
        "classes": classes,
        "kinds": _span_kinds(list(decision.spans)),
        "rule_ids": clean_rule_ids(list(getattr(decision, "rule_ids", None) or [])),
        "cue": _cue_of(text),
        "site": clean_site(site),
        "source": clean_source(source),
        "fingerprints": prints,
        "domain_prints": _domain_prints(text, list(decision.spans)),
        "latency_ms": latency if isinstance(latency, dict) else {"total": latency},
    }
    evidence = decision.evidence or {}
    for _key in ("protected_key", "role", "left_machine", "auto_rule"):
        if _key in evidence:
            record[_key] = evidence[_key]
    return _append_record(log_path, record)


def append_choice(
    log_path: str | None,
    *,
    decision_id: str,
    choice: str,
    kinds: list[str] | tuple[str, ...] | None = None,
    rule_ids: list[str] | tuple[str, ...] | None = None,
    site: str = "app",
    source: str = "text",
) -> dict[str, Any]:
    """Append one habit choice event (fields only, never values)."""
    record: dict[str, Any] = {
        "event": "choice",
        "ts": datetime.now(timezone.utc).isoformat(),
        "decision_id": clean_decision_id(decision_id),
        "choice": clean_choice(choice),
        "kinds": clean_kinds(kinds),
        "rule_ids": clean_rule_ids(rule_ids),
        "site": clean_site(site),
        "source": clean_source(source),
    }
    return _append_record(log_path, record)


def append_rule_change(
    log_path: str | None,
    *,
    suggestion_id: str,
    type: str,
    target: str,
    action: str,
    site: str = "",
) -> dict[str, Any]:
    """Append one rule-change event for an applied/undone suggestion."""
    action_text = str(action or "").strip().lower()
    if action_text not in ("applied", "undone"):
        raise ValueError(f"bad rule_change action {str(action)[:32]!r}")
    type_text = str(type or "").strip().lower()
    if not re.fullmatch(r"[a-z_]{1,32}", type_text):
        raise ValueError(f"bad rule_change type {str(type)[:32]!r}")
    target_text = str(target or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:@ /-]{1,96}", target_text):
        raise ValueError("bad rule_change target")
    suggestion_text = str(suggestion_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", suggestion_text):
        raise ValueError("bad suggestion_id")
    record: dict[str, Any] = {
        "event": "rule_change",
        "ts": datetime.now(timezone.utc).isoformat(),
        "suggestion_id": suggestion_text,
        "type": type_text,
        "target": target_text,
        "action": action_text,
        "site": clean_site(site) if str(site or "").strip() else "",
    }
    return _append_record(log_path, record)


def append_history_cleared(log_path: str | None) -> dict[str, Any]:
    """Append the chained "history cleared" marker; return it."""
    record: dict[str, Any] = {
        "event": "history_cleared",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    return _append_record(log_path, record)


def clear_history(log_path: str | None = None) -> dict[str, Any]:
    """Delete history: chain a marker, then truncate to that marker.

    The marker stays chained to the deleted tail, and :func:`verify`
    accepts it as a new starting point, so the remaining chain still
    verifies.
    """
    marker = append_history_cleared(log_path)
    path = Path(resolve_path(log_path))
    path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    return marker


def read_events(log_path: str | None = None) -> list[dict[str, Any]]:
    """Return every well-formed event in the log (oldest first)."""
    path = Path(resolve_path(log_path))
    events: list[dict[str, Any]] = []
    if not path.is_file():
        return events
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return events
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                events.append(record)
    return events


def verify(log_path: str | None = None) -> tuple[bool, int]:
    """Recompute the hash chain; return ``(ok, event_count)``.

    A missing log file is an empty intact chain: ``(True, 0)``. A
    leading ``history_cleared`` marker is accepted as a new starting
    point (it stays chained to the deleted tail).
    """
    path = Path(resolve_path(log_path))
    if not path.is_file():
        return True, 0
    prev = GENESIS_HASH
    count = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("prev_hash") != prev:
                    if not (
                        count == 0
                        and record.get("event") == "history_cleared"
                    ):
                        return False, count + 1
                stored = record.get("hash", "")
                check = dict(record)
                check.pop("hash", None)
                if _hash_record(check) != stored:
                    return False, count + 1
                prev = stored
                count += 1
    except (ValueError, KeyError, TypeError):
        return False, count
    return True, count


def verify_chain(log_path: str | None = None) -> bool:
    """Return True when the chain is intact (ignores the event count)."""
    ok, _ = verify(log_path)
    return ok
