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

# Generic human labels for placeholder bases (e.g. "[PII_EMAIL_1]" ->
# "PII_EMAIL"). Used only for user-facing text; machine reasons in
# ``Decision.reasons`` keep span ids for the log.
PLACEHOLDER_DESCRIPTIONS = {
    "SECRET": "secret",
    "PII_EMAIL": "email address",
    "PII_PHONE": "phone number",
    "CODENAME": "internal codename",
    "CLIENT": "client name",
    "FIGURE": "unreleased figure",
    "BEACON": "possible sensitive pointer",
}

_PLURALS = {
    "email address": "email addresses",
}


def _plural(count: int, singular: str) -> str:
    if count == 1:
        return f"1 {singular}"
    return f"{count} {_PLURALS.get(singular, singular + 's')}"


def _placeholder_base(placeholder: str) -> str:
    """Return the base label of a canonical ``[LABEL_N]`` placeholder."""
    inner = placeholder.strip().strip("[]")
    base, _, number = inner.rpartition("_")
    if not base or not number.strip().isdigit():
        return inner
    return base


def describe_placeholders(placeholders: dict[str, str]) -> list[str]:
    """Count placeholders by human label, e.g. ``["1 email address"]``.

    Order follows first appearance. Unknown labels fall back to a
    lower-cased, space-separated form so no raw token ever leaks.
    """
    counts: dict[str, int] = {}
    for placeholder in placeholders:
        base = _placeholder_base(placeholder)
        label = PLACEHOLDER_DESCRIPTIONS.get(
            base, base.replace("_", " ").lower() or "redacted value"
        )
        counts[label] = counts.get(label, 0) + 1
    return [_plural(n, label) for label, n in counts.items()]


def user_message(decision: Decision) -> str:
    """One plain user-facing sentence for a decision.

    Contains no span ids, hashes, or raw values: those stay in
    ``decision.reasons`` (the log), never in the banner.
    """
    if decision.decision == "BLOCK":
        return (
            "A planted canary token was found (proven by code). "
            "Nothing was sent."
        )
    if decision.decision == "REDACT":
        total = len(decision.placeholders)
        if total == 0:
            return "Sent with redactions."
        parts = describe_placeholders(decision.placeholders)
        noun = "redaction" if total == 1 else "redactions"
        return f"Sent with {total} {noun}: " + ", ".join(parts) + "."
    if decision.decision == "HOLD":
        return "Held for review: possible pointer to sensitive data."
    return "Sent clean"


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
__all__ = [
    "Decision",
    "DECISIONS",
    "describe_placeholders",
    "gate",
    "user_message",
]
