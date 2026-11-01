"""Tier-1-only gate: code findings in, a typed decision out.

Severity ordering is BLOCK > HOLD > REDACT > ALLOW. A proven canary
blocks; ambiguous spans or sensitivity cue words hold instantly for
asynchronous Glimmer whole-message review (see ``holds``); other
proven spans redact and clean text allows. Any internal failure holds
(fail closed). The send path never calls a model: it returns HOLD
fast and review happens in the background.
"""

from __future__ import annotations

import re
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
    "PII_ACCOUNT": "account number",
    "PII_ROUTING": "routing number",
    "PII_IBAN": "IBAN",
    "PII_SWIFT": "SWIFT code",
    "PII_SORT_CODE": "sort code",
    "PII_HOLDER_NAME": "account holder name",
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
    ``decision.reasons`` (the log), never in the banner. When a
    document-type warning is present it is shown first, above the
    decision line.
    """
    if decision.decision == "BLOCK":
        base = (
            "A planted canary token was found (proven by code). "
            "Nothing was sent."
        )
    elif decision.decision == "REDACT":
        total = len(decision.placeholders)
        if total == 0:
            base = "Sent with redactions."
        else:
            parts = describe_placeholders(decision.placeholders)
            noun = "redaction" if total == 1 else "redactions"
            base = f"Sent with {total} {noun}: " + ", ".join(parts) + "."
    elif decision.decision == "HOLD":
        base = "Held for review: possible pointer to sensitive data."
    else:
        base = "Sent clean"
    warning = getattr(decision, "doc_warning", None)
    if isinstance(warning, str) and warning:
        return f"{warning} {base}"
    return base


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
    doc_type: str | None = None  # doc warning type, never blocks
    doc_warning: str | None = None  # plain warning above the decision


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


# Sensitivity cue words for the send-time hold rule. Tier-1-clean text
# holding none of these (and no ambiguous spans) is decided from Tier 1
# alone; anything else holds for asynchronous Glimmer review.
_SCREEN_CUE_RE = re.compile(
    r"(?i)\b(password|passwd|creds|credentials?|secret|token|keys?|keystore"
    r"|vault|ssn|social\s+security|dob|date\s+of\s+birth|born|address"
    r"|salary|medical|diagnosis|customer\s+list|hr|payroll|share"
    r"|drive\s+folder|bucket|nas)\b"
)
_SCREEN_UNC_RE = re.compile(r"\\\\[A-Za-z0-9_.$-]+")

MESSAGE_PENDING_CHARS = 2000


def has_sensitivity_cue(text: str) -> bool:
    """True when text holds a sensitivity cue word or UNC server path."""
    return (
        _SCREEN_CUE_RE.search(text) is not None
        or _SCREEN_UNC_RE.search(text) is not None
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

    Ambiguous spans or a sensitivity cue word HOLD instantly at
    resolved tier 4 (status ``reviewing``) with ``evidence`` carrying
    ``proven_by_code`` span ids and ``pending`` items: one
    ``{kind: "message", text}`` whole-message item plus one
    ``{kind: "span", ...}`` item per ambiguous span;
    ``holds.start_review`` reviews them in a background thread.
    Proven-only input redacts and clean input allows, both with no
    model call.
    """
    if ambiguous or has_sensitivity_cue(text):
        latency_ms["tier2"] = 0.0
        latency_ms["tier3"] = 0.0
        latency_ms["total"] = (time.perf_counter() - total_started) * 1000
        if ambiguous:
            reasons.append(
                f"{len(ambiguous)} ambiguous span(s) need Glimmer review -> HOLD"
            )
        else:
            reasons.append("sensitivity cue word needs Glimmer review -> HOLD")
        pending: list[dict[str, Any]] = [
            {"kind": "message", "text": text[:MESSAGE_PENDING_CHARS]}
        ]
        for span in ambiguous:
            pending.append(
                {
                    "kind": "span",
                    "span_id": span.span_id,
                    "text": text[span.cp_start:span.cp_end],
                }
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
                "pending": pending,
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
def _attach_doc_classification(
    decision: Decision,
    text: str,
    trace_out: list[dict[str, Any]] | None,
) -> None:
    """Classify the document type and attach its warning (T29).

    The warning is informational only: it is stored on the decision,
    copied into the decision evidence, prepended to the user-facing
    message, and recorded as a ``doc.classify`` trace step. The gate
    decision itself is never changed here.
    """
    from . import docclass as docclass_module
    try:
        started = time.perf_counter()
        info = docclass_module.classify(text)
        elapsed_ms = (time.perf_counter() - started) * 1000
    except Exception:
        return
    doc_type = info.get("doc_type")
    scores = info.get("scores") or {}
    if not isinstance(doc_type, str) or not doc_type:
        if trace_out is not None:
            trace_out.append(
                {"step": "doc.classify", "tool": "tier1",
                 "detail": "no document type",
                 "result": "none", "ms": round(elapsed_ms, 3)}
            )
        return
    warning = info.get("warning")
    decision.doc_type = doc_type
    decision.doc_warning = warning if isinstance(warning, str) else None
    evidence = dict(decision.evidence or {})
    evidence["doc_type"] = doc_type
    if decision.doc_warning:
        evidence["doc_warning"] = decision.doc_warning
    evidence["doc_signals"] = [
        f"{dtype}:{name}+{weight}"
        for dtype, name, weight in (info.get("signals") or [])
        if dtype == doc_type
    ]
    decision.evidence = evidence
    if trace_out is not None:
        detail = f"{doc_type} (score {scores.get(doc_type, 0)}): " + ", ".join(
            evidence["doc_signals"]
        )
        trace_out.append(
            {"step": "doc.classify", "tool": "tier1",
             "detail": detail, "result": doc_type,
             "ms": round(elapsed_ms, 3)}
        )


def _gate_impl(
    text: str,
    policy: Policy,
    registry: CanaryRegistry | None = None,
    choices: list[dict[str, Any]] | None = None,
    mode: str = "tier1",
    trace_out: list[dict[str, Any]] | None = None,
) -> Decision:
    """Run the gate over text and return a Decision.

    ``choices`` is a list of ``{span_id, choice}`` where choice is
    ``redact`` or ``keep``. ``keep`` is honoured only when the policy
    allows keeping that span class, and never for secrets.

    ``mode="tier1"`` (default) resolves ambiguous spans to HOLD at
    tier 1. ``mode="send"`` HOLDS instantly at resolved tier 4
    (status ``reviewing``) when Tier 1 finds ambiguous spans or the
    message holds a sensitivity cue word: ``holds.start_review``
    judges the whole message once with Glimmer plus each ambiguous
    span in a background thread and verifies the answer in code.
    Proven-only input redacts and clean input allows, with no model
    call.
    """
    total_started = time.perf_counter()
    try:
        scan_started = time.perf_counter()
        spans = tier1.scan(text, policy, registry, trace_out=trace_out)
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
        custom_block = [
            s for s in enforced
            if s.status == "proven" and s.proof == "custom"
            and s.suggested_action == "block"
        ]
        if custom_block:
            reasons.append(
                f"{len(custom_block)} custom rule span(s) "
                "with action block -> BLOCK"
            )
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
        custom_review = [
            s for s in enforced
            if s.status == "proven" and s.proof == "custom"
            and s.suggested_action == "review"
        ]
        if custom_review:
            reasons.append(
                f"{len(custom_review)} custom rule span(s) "
                "with action review -> HOLD"
            )
            if mode == "send":
                latency_ms["tier2"] = 0.0
                latency_ms["tier3"] = 0.0
                latency_ms["total"] = (
                    time.perf_counter() - total_started
                ) * 1000
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
                        "pending": [
                            {
                                "kind": "message",
                                "text": text[:MESSAGE_PENDING_CHARS],
                            }
                        ],
                    },
                    status="reviewing",
                )
            return _hold(spans, latency_ms, reasons)
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


def _trace_step(
    trace: list[dict[str, Any]],
    step: str,
    tool: str,
    detail: str,
    result: str,
    ms: float = 0.0,
) -> None:
    """Append one descriptive-mode step (counts and labels, never raw text)."""
    trace.append(
        {"step": step, "tool": tool, "detail": detail,
         "result": result, "ms": ms}
    )


def _append_gate_trace(
    trace_out: list[dict[str, Any]] | None,
    decision: Decision,
    choices: list[dict[str, Any]] | None,
    total_ms: float,
) -> None:
    """Post-hoc gate steps built from the finished decision.

    Tier 1 detector steps (when asked for) are already in ``trace_out``;
    here come triage, redaction, background-review, and decision steps.
    Placeholder labels such as ``[PII_EMAIL_1]`` are safe to list: they
    hold no raw values.
    """
    if trace_out is None:
        return
    proven = sum(1 for s in decision.spans if s.status == "proven")
    ambiguous = sum(1 for s in decision.spans if s.status == "ambiguous")
    _trace_step(
        trace_out, "gate.triage", "gate",
        f"{proven} proven, {ambiguous} ambiguous", "triaged",
    )
    if choices:
        _trace_step(
            trace_out, "gate.choices", "gate",
            f"{len(choices)} choice(s) recorded", "applied",
        )
    if decision.placeholders:
        labels = ", ".join(sorted(decision.placeholders))
        _trace_step(
            trace_out, "redact.apply", "redact",
            f"{len(decision.placeholders)} placeholder(s): {labels}",
            "redacted",
        )
    if decision.status == "reviewing":
        _trace_step(
            trace_out, "glimmer.review", "glimmer",
            "full message queued for background review", "reviewing",
        )
    _trace_step(
        trace_out, "decision", "gate",
        f"resolved tier {decision.resolved_tier}", decision.decision,
        round(total_ms, 3),
    )


def gate(
    text: str,
    policy: Policy,
    registry: CanaryRegistry | None = None,
    choices: list[dict[str, Any]] | None = None,
    mode: str = "tier1",
    trace_out: list[dict[str, Any]] | None = None,
) -> Decision:
    """Run the gate over text and return a Decision.

    ``choices`` is a list of ``{span_id, choice}`` where choice is
    ``redact`` or ``keep``. ``keep`` is honoured only when the policy
    allows keeping that span class, and never for secrets.

    ``mode="tier1"`` (default) resolves ambiguous spans to HOLD at
    tier 1. ``mode="send"`` HOLDS instantly at resolved tier 4
    (status ``reviewing``) when Tier 1 finds ambiguous spans or the
    message holds a sensitivity cue word: ``holds.start_review``
    judges the whole message once with Glimmer plus each ambiguous
    span in a background thread and verifies the answer in code.
    Proven-only input redacts and clean input allows, with no model
    call.

    Custom rules (``policy.custom_rules``) apply as Tier 1 proven
    spans with their own action: ``block`` blocks, ``review`` holds,
    ``redact`` redacts.

    When ``trace_out`` is a list, a step-by-step descriptive trace of
    ``{step, tool, detail, result, ms}`` entries is appended: one per
    Tier 1 detector plus triage/redaction/review/decision steps. The
    trace holds counts and placeholder-style labels only, never raw
    sensitive values, and is never written to the decision log.
    """
    started = time.perf_counter()
    decision = _gate_impl(
        text, policy, registry, choices, mode, trace_out=trace_out
    )
    _attach_doc_classification(decision, text, trace_out)
    _append_gate_trace(
        trace_out, decision, choices,
        (time.perf_counter() - started) * 1000,
    )
    return decision


# Re-export for callers that only import the engine.
__all__ = [
    "Decision",
    "DECISIONS",
    "describe_placeholders",
    "gate",
    "user_message",
]
