"""Hash-chained JSONL decision log (T7).

Every gate decision is appended as one JSON object per line to the file at
``SF_LOG_PATH`` (default ``runs/decisions.jsonl``). Each event carries::

    {ts, decision_id, decision, tier, classes, fingerprints,
     latency_ms, prev_hash, hash}

``fingerprints`` are ``sha256(value)[:12]`` of each flagged span's raw
value; raw values are never written. ``hash`` chains to the previous
event so tampering is detectable: :func:`verify` recomputes the chain
and returns ``(ok, n)``.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Decision

DEFAULT_LOG_PATH = "runs/decisions.jsonl"
GENESIS_HASH = "0" * 64


def resolve_path(explicit: str | None = None) -> str:
    """Return the log path: explicit arg, ``SF_LOG_PATH``, or the default."""
    if explicit:
        return explicit
    return os.environ.get("SF_LOG_PATH", DEFAULT_LOG_PATH)


def fingerprint(value: str) -> str:
    """Short fingerprint of one raw value; the value itself never logs."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _span_value(text: str, cp_start: int, cp_end: int) -> str:
    """Slice one span's raw value out of the gated text (code points)."""
    if 0 <= cp_start < cp_end <= len(text):
        return text[cp_start:cp_end]
    return ""


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


def append_decision(
    log_path: str | None,
    decision: Decision,
    text: str = "",
) -> dict[str, Any]:
    """Append one gate decision; return the stored record.

    ``text`` is the gated user text, used only to fingerprint span
    values — it is never written to the log.
    """
    path = Path(resolve_path(log_path))
    if path.parent != Path("") and str(path.parent):
        path.parent.mkdir(parents=True, exist_ok=True)
    prev_hash, _ = _last_hash(path)
    classes = sorted({span.cls for span in decision.spans})
    prints = [
        fingerprint(_span_value(text, span.cp_start, span.cp_end))
        for span in decision.spans
    ]
    latency = decision.latency_ms
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "decision_id": decision.decision_id,
        "decision": decision.decision,
        "tier": decision.resolved_tier,
        "classes": classes,
        "fingerprints": prints,
        "latency_ms": latency if isinstance(latency, dict) else {"total": latency},
        "prev_hash": prev_hash,
    }
    evidence = decision.evidence or {}
    for _key in ("protected_key", "role", "left_machine"):
        if _key in evidence:
            record[_key] = evidence[_key]
    record["hash"] = _hash_record(record)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def verify(log_path: str | None = None) -> tuple[bool, int]:
    """Recompute the hash chain; return ``(ok, event_count)``.

    A missing log file is an empty intact chain: ``(True, 0)``.
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
