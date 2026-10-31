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

from . import judge as judge_module
from . import tier1
from . import verifier as verifier_module
from .canary import CanaryRegistry
from .config import get_settings
from .policy import Policy
from .redact import redact, save_mapping
from .spans import Span, merge_overlaps

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
    evidence: dict[str, Any] = field(default_factory=dict)


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


def _judgement_dump(judgement: Any) -> dict[str, Any] | None:
    """Serialisable form of one judgement for HOLD evidence."""
    if judgement is None:
        return None
    if hasattr(judgement, "model_dump"):
        try:
            return judgement.model_dump()
        except Exception:
            pass
    if isinstance(judgement, dict):
        return dict(judgement)
    return {"repr": repr(judgement)[:200]}


def _gate_send(
    text: str,
    policy: Policy,
    registry: CanaryRegistry | None,
    spans: list[Span],
    proven: list[Span],
    ambiguous: list[Span],
    latency_ms: dict[str, float],
    reasons: list[str],
    total_started: float,
) -> Decision:
    """Send-time path: judge each ambiguous span, verify in code.

    A confirmed span redacts (blocks when it resolves to a canary); a
    model-benign span with confidence >= 0.8 and sensitivity < 0.3 is
    allowed; anything else holds for a human at resolved tier 4. A
    model answer can never downgrade a Tier 1 proven span.
    """
    model = get_settings().judge_model
    tier2_ms = 0.0
    tier3_ms = 0.0
    confirmed: list[Span] = []
    canary_hit = False
    suspicions: list[dict[str, Any]] = []
    if not ambiguous:
        latency_ms["tier2"] = 0.0
        latency_ms["tier3"] = 0.0
        latency_ms["total"] = (time.perf_counter() - total_started) * 1000
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
    for span in ambiguous:
        window = judge_module.make_window(text, span.cp_start, span.cp_end)
        judge_started = time.perf_counter()
        try:
            judgement = judge_module.judge(window, model, live=False)
        except Exception:
            judgement = None
        tier2_ms += (time.perf_counter() - judge_started) * 1000
        if judgement is None:
            reasons.append(f"{span.span_id} judge unavailable -> HOLD")
            suspicions.append(
                {
                    "span": text[span.cp_start:span.cp_end],
                    "judgement": None,
                    "why_unverified": "judge unavailable",
                }
            )
            continue
        verify_started = time.perf_counter()
        try:
            verdict = verifier_module.verify(
                text, judgement, spans, policy, registry
            )
        except Exception as exc:
            tier3_ms += (time.perf_counter() - verify_started) * 1000
            reasons.append(
                f"{span.span_id} verification failed "
                f"({type(exc).__name__}) -> HOLD"
            )
            suspicions.append(
                {
                    "span": text[span.cp_start:span.cp_end],
                    "judgement": _judgement_dump(judgement),
                    "why_unverified": f"verification failed: {type(exc).__name__}",
                }
            )
            continue
        tier3_ms += (time.perf_counter() - verify_started) * 1000
        if verdict.confirmed and verdict.span is not None:
            if verdict.span.subclass == "canary":
                canary_hit = True
            confirmed.append(verdict.span)
            reasons.append(
                f"{span.span_id} tier3 confirmed ({verdict.cls}) -> "
                f"{'BLOCK' if verdict.span.subclass == 'canary' else 'redact'}"
            )
            continue
        sensitivity = getattr(judgement, "sensitivity", 1.0)
        if isinstance(judgement, dict):
            sensitivity = judgement.get("sensitivity", 1.0)
        confidence = getattr(judgement, "confidence", 0.0)
        if isinstance(judgement, dict):
            confidence = judgement.get("confidence", 0.0)
        try:
            benign_clear = (
                getattr(judgement, "cls", None)
                if not isinstance(judgement, dict)
                else judgement.get("cls")
            ) == "benign" and float(confidence) >= 0.8 and float(sensitivity) < 0.3
        except (TypeError, ValueError):
            benign_clear = False
        if benign_clear:
            reasons.append(
                f"{span.span_id} model benign "
                f"(confidence {confidence}, sensitivity {sensitivity}) "
                f"-> allow"
            )
            continue
        reasons.append(f"{span.span_id} unverified ({verdict.why}) -> HOLD")
        suspicions.append(
            {
                "span": text[span.cp_start:span.cp_end],
                "judgement": _judgement_dump(judgement),
                "why_unverified": verdict.why,
            }
        )
    latency_ms["tier2"] = tier2_ms
    latency_ms["tier3"] = tier3_ms
    latency_ms["total"] = (time.perf_counter() - total_started) * 1000
    if suspicions:
        reasons.append(f"{len(suspicions)} span(s) need human review -> HOLD")
        return Decision(
            decision="HOLD",
            decision_id=uuid.uuid4().hex,
            spans=spans,
            sent_text=None,
            placeholders={},
            resolved_tier=4,
            latency_ms=latency_ms,
            reasons=reasons,
            evidence={
                "proven_by_code": [s.span_id for s in proven],
                "model_suspicion_only": suspicions,
            },
        )
    if canary_hit:
        reasons.append("model-confirmed span resolves to a canary -> BLOCK")
        return Decision(
            decision="BLOCK",
            decision_id=uuid.uuid4().hex,
            spans=spans,
            sent_text=None,
            placeholders={},
            resolved_tier=3,
            latency_ms=latency_ms,
            reasons=reasons,
        )
    combined = merge_overlaps(proven + confirmed)
    if combined:
        sent_text, mapping = redact(text, combined)
        decision_id = uuid.uuid4().hex
        save_mapping(decision_id, mapping)
        reasons.append(
            f"{len(combined)} span(s) redacted "
            f"({len(mapping)} placeholder(s)) -> REDACT"
        )
        return Decision(
            decision="REDACT",
            decision_id=decision_id,
            spans=spans,
            sent_text=sent_text,
            placeholders=dict(mapping),
            resolved_tier=3,
            latency_ms=latency_ms,
            reasons=reasons,
        )
    reasons.append("model cleared every ambiguous span -> ALLOW")
    return Decision(
        decision="ALLOW",
        decision_id=uuid.uuid4().hex,
        spans=spans,
        sent_text=text,
        placeholders={},
        resolved_tier=3,
        latency_ms=latency_ms,
        reasons=reasons,
    )


def gate(
    text: str,
    policy: Policy,
    registry: CanaryRegistry | None = None,
    choices: list[dict[str, Any]] | None = None,
    mode: str = "tier1",
) -> Decision:
    """Run the gate over text and return a Decision.

    ``choices`` is a list of ``{span_id, choice}`` where choice is
    ``redact`` or ``keep``. ``keep`` is honoured only when the policy
    allows keeping that span class, and never for secrets.

    ``mode="tier1"`` (default) resolves ambiguous spans to HOLD at
    tier 1. ``mode="send"`` asks the Tier 2 judge about each ambiguous
    span and verifies the answer in code (Tier 3): confirmed spans
    redact (or block on a canary), model-cleared spans are allowed, and
    anything else holds at resolved tier 4.
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
            if mode == "send":
                latency_ms["tier2"] = 0.0
                latency_ms["tier3"] = 0.0
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
        if mode == "send":
            return _gate_send(
                text, policy, registry, spans, proven, ambiguous,
                latency_ms, reasons, total_started,
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
        decision = _hold([], latency_ms, [f"internal_error: {type(exc).__name__}"])
        if mode == "send":
            decision.resolved_tier = 4
            decision.latency_ms["tier2"] = 0.0
            decision.latency_ms["tier3"] = 0.0
        return decision


# Re-export for callers that only import the engine.
__all__ = [
    "Decision",
    "DECISIONS",
    "describe_placeholders",
    "gate",
    "user_message",
]
