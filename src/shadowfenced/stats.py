"""Decision stats (T7): counters plus per-tier latency summaries.

Stats are derived from the hash-chained decision log so they survive
restarts: :func:`snapshot` reads the log file at ``SF_LOG_PATH`` and
returns ``{total, decisions, by_tier, last_latency_ms, p50_latency_ms,
p95_latency_ms}``. The in-memory :class:`Stats` collector backs unit
tests and live tallying.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .ecs_log import resolve_path


def _percentile(sorted_values: list[float], rank: float) -> float:
    """Nearest-rank percentile of pre-sorted values; 0.0 when empty."""
    if not sorted_values:
        return 0.0
    index = min(int(rank * len(sorted_values)), len(sorted_values) - 1)
    return sorted_values[index]


class Stats:
    """In-memory tally of decisions with per-tier latency summaries."""

    def __init__(self) -> None:
        self.decisions: Counter[str] = Counter()
        self.by_tier: Counter[str] = Counter()
        self._latencies: dict[str, list[float]] = {}
        self._last: dict[str, float] = {}

    def record(self, decision: str, tier: int, latency_ms: float) -> None:
        """Tally one decision with its resolved tier and total latency."""
        key = str(tier)
        self.decisions[decision] += 1
        self.by_tier[key] += 1
        self._latencies.setdefault(key, []).append(float(latency_ms))
        self._last[key] = float(latency_ms)

    def snapshot(self) -> dict[str, Any]:
        """Return the JSON-serialisable stats snapshot."""
        p50: dict[str, float] = {}
        p95: dict[str, float] = {}
        for tier, values in self._latencies.items():
            ordered = sorted(values)
            p50[tier] = _percentile(ordered, 0.50)
            p95[tier] = _percentile(ordered, 0.95)
        return {
            "total": sum(self.decisions.values()),
            "decisions": dict(self.decisions),
            "by_tier": dict(self.by_tier),
            "last_latency_ms": dict(self._last),
            "p50_latency_ms": p50,
            "p95_latency_ms": p95,
        }


def _event_latency(event: dict[str, Any]) -> float:
    latency = event.get("latency_ms", 0.0)
    if isinstance(latency, dict):
        total = latency.get("total", 0.0)
        return float(total) if isinstance(total, (int, float)) else 0.0
    return float(latency) if isinstance(latency, (int, float)) else 0.0


def stats_from_log(log_path: str | None = None) -> dict[str, Any]:
    """Build a stats snapshot from the JSONL decision log.

    One decision per ``decision_id``: a HOLD that later resolves logs
    its resolution under the same id, so the latest event wins instead
    of counting the HOLD twice. ``held_then_resolved`` keeps the
    "held first" count visible as a separate counter.
    """
    latest: dict[str, dict[str, Any]] = {}
    first: dict[str, str] = {}
    order: list[str] = []
    path = Path(resolve_path(log_path))
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                decision = event.get("decision")
                if not isinstance(decision, str):
                    continue
                key = event.get("decision_id")
                if not isinstance(key, str) or not key:
                    key = f"__noid_{len(order)}"
                if key not in latest:
                    order.append(key)
                    first[key] = decision
                latest[key] = event
    collector = Stats()
    for key in order:
        event = latest[key]
        tier = event.get("tier", 1)
        try:
            tier_int = int(tier)
        except (TypeError, ValueError):
            tier_int = 1
        decision = event.get("decision")
        assert isinstance(decision, str)
        collector.record(decision, tier_int, _event_latency(event))
    snap = collector.snapshot()
    snap["held_then_resolved"] = sum(
        1 for key in order
        if first[key] == "HOLD" and latest[key].get("decision") != "HOLD"
    )
    return snap


def snapshot(log_path: str | None = None) -> dict[str, Any]:
    """Snapshot alias used by the ``/api/stats`` route."""
    return stats_from_log(log_path)
