"""Tier-1-only gate: code findings in, a typed decision out.

Severity ordering is BLOCK > HOLD > REDACT > ALLOW. A proven canary
blocks; other proven spans redact; ambiguous spans hold for the Tier 2
judge that lands later; clean text allows. Any internal failure holds
(fail closed).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import tier1
from .canary import CanaryRegistry
from .policy import Policy
from .redact import redact, save_mapping
from .spans import Span

DECISIONS = ("ALLOW", "REDACT", "BLOCK", "HOLD")


@dataclass
class Decision:
    """One gate outcome."""

    decision: str  # ALLOW | REDACT | BLOCK | HOLD
    decision_id: str  # uuid4 hex
    spans: list[Span] = field(default_factory=list)
    sent_text: str | None = None  # redacted text when sent, else None
    placeholders: dict[str, str] = field(default_factory=dict)
    resolved_tier: int = 1
    latency_ms: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _hold(
    spans: list[Span],
    latency_ms: dict[str, float],
    reasons: list[str],
) -> Decision:
    return Decision(
        decision="HOLD",
        decision_id=uuid.uuid4().hex,
        spans=spans,
        sent_text=None,
        placeholders={},
        resolved_tier=1,
        latency_ms=latency_ms,
        reasons=reasons,
    )


def gate(
    text: str,
    policy: Policy,
    registry: CanaryRegistry | None = None,
    choices: list[dict[str, Any]] | None = None,
) -> Decision:
    """Run the Tier 1 gate over text and return a Decision.

    ``choices`` is a list of ``{span_id, choice}`` where choice is
    ``redact`` or ``keep``. ``keep`` is honoured only when the policy
    allows keeping that span class, and never for secrets.
    """
    total_started = time.perf_counter()
    try:
        scan_started = time.perf_counter()
        spans = tier1.scan(text, policy, registry)
        tier1_ms = (time.perf_counter() - scan_started) * 1000
        latency_ms = {
            "tier1": tier1_ms,
            "total": (time.perf_counter() - total_started) * 1000,
        }
        reasons: list[str] = []

        kept: set[str] = set()
        for item in choices or []:
            span_id = item.get("span_id", "")
            choice = item.get("choice", "")
            span = next((s for s in spans if s.span_id == span_id), None)
            if span is None:
                reasons.append(f"choice for unknown span {span_id} ignored")
                continue
            if choice == "keep":
                if span.cls == "secret" or not policy.keepable(span.cls):  # type: ignore[arg-type]
                    reasons.append(
                        f"keep denied for {span_id} "
                        f"({span.cls} cannot be kept)"
                    )
                else:
                    kept.add(span_id)
                    reasons.append(f"user kept {span_id} ({span.cls})")
            elif choice == "redact":
                reasons.append(f"user redacted {span_id} ({span.cls})")
            else:
                reasons.append(f"choice for {span_id} ignored: {choice!r}")

        enforced = [s for s in spans if s.span_id not in kept]
        proven = [s for s in enforced if s.status == "proven"]
        ambiguous = [s for s in enforced if s.status == "ambiguous"]

        if any(s.subclass == "canary" and s.status == "proven" for s in enforced):
            reasons.append("proven canary -> BLOCK")
            return Decision(
                decision="BLOCK",
                decision_id=uuid.uuid4().hex,
                spans=spans,
                sent_text=None,
                placeholders={},
                resolved_tier=1,
                latency_ms=latency_ms,
                reasons=reasons,
            )
        if ambiguous:
            reasons.append(
                f"{len(ambiguous)} ambiguous span(s) need Tier 2 review -> HOLD"
            )
            return Decision(
                decision="HOLD",
                decision_id=uuid.uuid4().hex,
                spans=spans,
                sent_text=None,
                placeholders={},
                resolved_tier=1,
                latency_ms=latency_ms,
                reasons=reasons,
            )
        if proven:
            sent_text, mapping = redact(text, proven)
            decision_id = uuid.uuid4().hex
            save_mapping(decision_id, mapping)
            reasons.append(
                f"{len(proven)} proven span(s) redacted "
                f"({len(mapping)} placeholder(s)) -> REDACT"
            )
            return Decision(
                decision="REDACT",
                decision_id=decision_id,
                spans=spans,
                sent_text=sent_text,
                placeholders=dict(mapping),
                resolved_tier=1,
                latency_ms=latency_ms,
                reasons=reasons,
            )
        reasons.append("no spans -> ALLOW")
        return Decision(
            decision="ALLOW",
            decision_id=uuid.uuid4().hex,
            spans=spans,
            sent_text=text,
            placeholders={},
            resolved_tier=1,
            latency_ms=latency_ms,
            reasons=reasons,
        )
    except Exception as exc:  # fail closed: never allow on error
        latency_ms = {
            "tier1": 0.0,
            "total": (time.perf_counter() - total_started) * 1000,
        }
        return _hold([], latency_ms, [f"internal_error: {type(exc).__name__}"])


# Re-export for callers that only import the engine.
__all__ = ["Decision", "DECISIONS", "gate"]
