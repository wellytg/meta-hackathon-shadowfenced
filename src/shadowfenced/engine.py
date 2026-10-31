"""Tier-1-only gate: code findings in, a typed decision out.

Severity ordering is BLOCK > HOLD > REDACT > ALLOW. A proven canary
blocks; other proven spans redact; ambiguous spans hold instantly for
asynchronous Glimmer review (see ``holds``); clean text allows. Any
internal failure holds (fail closed). The send path runs a
whole-message live-model screen when Tier 1 finds no BLOCK and returns
HOLD fast; review happens in the background.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import tier1
from .canary import CanaryRegistry
from .config import get_settings
from .policy import Policy
from .redact import redact, save_mapping
from .spans import Span, span_from_cp_range

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
    status: str = "complete"  # complete | reviewing (async Glimmer review)


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


# Sensitivity cue words for the send-time screen fail-closed rule.
# A screen timeout or error allows only Tier-1-clean text with none of
# these; anything else holds for a human.
_SCREEN_CUE_RE = re.compile(
    r"(?i)\b(password|passwd|creds|credentials?|secret|token|keys?|keystore"
    r"|vault|ssn|social\s+security|dob|date\s+of\s+birth|born|address"
    r"|salary|medical|diagnosis|customer\s+list|hr|payroll|share"
    r"|drive\s+folder|bucket|nas)\b"
)
_SCREEN_UNC_RE = re.compile(r"\\\\[A-Za-z0-9_.$-]+")

SCREEN_SPAN_CHARS = 1500


def has_sensitivity_cue(text: str) -> bool:
    """True when text holds a sensitivity cue word or UNC server path."""
    return (
        _SCREEN_CUE_RE.search(text) is not None
        or _SCREEN_UNC_RE.search(text) is not None
    )


def _screen_cue_span(
    text: str, policy: Policy,
) -> Span:
    """One ambiguous span scoping a screen fail-closed HOLD."""
    cue = _SCREEN_CUE_RE.search(text) or _SCREEN_UNC_RE.search(text)
    if cue is not None:
        start = text.rfind("\n", 0, cue.start()) + 1
        stop = text.find("\n", cue.end())
        end = len(text) if stop < 0 else stop
        end = min(end, start + SCREEN_SPAN_CHARS) or cue.end()
    else:
        start, end = 0, min(len(text), SCREEN_SPAN_CHARS)
    return span_from_cp_range(
        text,
        start,
        end,
        cls="beacon",
        subclass="screen_cue",
        tier=2,
        proof="code",
        status="ambiguous",
        message="Live screen unavailable and the text has sensitivity cues.",
        why="screen timeout/error with cue words or Tier 1 spans present",
        suggested_action="review",
        can_keep=policy.keepable("beacon"),  # type: ignore[arg-type]
        confidence=0.5,
    )


def _screen_suspicion_span(
    text: str, policy: Policy, cls: str, span_text: str, confidence: float,
) -> Span:
    """One ambiguous span for a suspicious whole-message screen verdict."""
    if span_text and span_text in text:
        start = text.find(span_text)
        end = start + len(span_text)
    else:
        start, end = 0, min(len(text), SCREEN_SPAN_CHARS)
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.5
    return span_from_cp_range(
        text,
        start,
        end,
        cls=cls,
        subclass="screen",
        tier=2,
        proof="model",
        status="ambiguous",
        message="Live screen flagged a possible sensitive span.",
        why="whole-message screen verdict with confidence >= 0.5",
        suggested_action="review",
        can_keep=policy.keepable(cls),  # type: ignore[arg-type]
        confidence=confidence_value,
    )


def _maybe_screen(
    text: str,
    policy: Policy,
    spans: list[Span],
    proven: list[Span],
    ambiguous: list[Span],
    latency_ms: dict[str, float],
    reasons: list[str],
    total_started: float,
) -> Decision | None:
    """Run the send-time whole-message screen; None means continue.

    Returns a HOLD decision when the screen verdict is suspicious or
    when the screen fails closed (timeout/error with Tier 1 spans or
    sensitivity cues present), else None so the normal path continues.
    """
    from . import judge as judge_module

    if not get_settings().screen_enabled:
        return None
    screen_started = time.perf_counter()
    try:
        judgement = judge_module.screen(text)
    except Exception:
        judgement = None
    latency_ms["tier2s"] = (time.perf_counter() - screen_started) * 1000
    if judgement is not None:
        cls = getattr(judgement, "cls", "benign")
        try:
            confidence = float(getattr(judgement, "confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        is_beacon = bool(getattr(judgement, "is_beacon", False))
        if (cls != "benign" and confidence >= 0.5) or is_beacon:
            effective = cls if cls != "benign" else "beacon"
            span_text = getattr(judgement, "span", "") or ""
            screen_span = _screen_suspicion_span(
                text, policy, effective, span_text, confidence
            )
            reasons.append(
                f"live screen suspicious ({effective}, "
                f"confidence {confidence:.2f}) -> HOLD"
            )
            return _gate_send(
                text, policy, None, spans + [screen_span], proven,
                ambiguous + [screen_span], latency_ms, reasons,
                total_started,
            )
        reasons.append("live screen benign -> Tier 1 path")
        return None
    if not spans and not has_sensitivity_cue(text):
        reasons.append("live screen unavailable but text clean -> Tier 1 path")
        return None
    reasons.append("live screen unavailable with sensitivity cues -> HOLD")
    extra: list[Span] = []
    if not ambiguous:
        extra = [_screen_cue_span(text, policy)]
    return _gate_send(
        text, policy, None, spans + extra, proven, ambiguous + extra,
        latency_ms, reasons, total_started,
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
    """Send-time path: never call the judge inline.

    Ambiguous spans HOLD instantly at resolved tier 4 (status
    ``reviewing``) with ``evidence`` carrying ``proven_by_code`` span
    ids and ``pending`` span texts; ``holds.start_review`` reviews
    them in a background thread. Proven-only input redacts and clean
    input allows, both with no model call.
    """
    if ambiguous:
        latency_ms["tier2"] = 0.0
        latency_ms["tier3"] = 0.0
        latency_ms["total"] = (time.perf_counter() - total_started) * 1000
        reasons.append(
            f"{len(ambiguous)} ambiguous span(s) need Glimmer review -> HOLD"
        )
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
                "pending": [text[s.cp_start:s.cp_end] for s in ambiguous],
            },
            status="reviewing",
        )
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
    tier 1. ``mode="send"`` first runs the whole-message live screen
    when Tier 1 finds no BLOCK, then HOLDS instantly at resolved tier 4
    (status ``reviewing``): ``holds.start_review`` judges each
    ambiguous span in a background thread and verifies the answer in
    code. Proven-only input redacts and clean input allows.
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
            screened = _maybe_screen(
                text, policy, spans, proven, ambiguous,
                latency_ms, reasons, total_started,
            )
            if screened is not None:
                return screened
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
