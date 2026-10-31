"""Asynchronous Glimmer review for HOLDs (T10).

``engine.gate(mode="send")`` never calls the judge inline: ambiguous
spans HOLD instantly and :func:`start_review` reviews them in a
background thread. For each pending span the send-model judge is asked
and the answer is verified in code (Tier 3):

- every pending span code-confirmed (and no canary anywhere) -> redact
  those spans too and auto-send (``redacted_sent``);
- every pending span model-benign with confidence >= 0.8 and
  sensitivity < 0.3, with no Tier 1 proven secret -> send
  (``released``; Tier 1 proven non-secrets stay redacted);
- anything else, judge None, or any exception -> ``needs_human`` with
  ``model_suspicion_only`` entries.

A canary value is never sent: any proven or confirmed canary forces
``needs_human`` instead of an auto-send. Every outcome is logged.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import judge as judge_module
from . import verifier as verifier_module
from .canary import CanaryRegistry
from .config import get_settings
from .engine import Decision, _judgement_dump, describe_placeholders
from .policy import Policy, load_policy
from .redact import rehydrate, save_mapping
from .spans import Span, merge_overlaps

REGISTRY_KINDS = ("db_password", "api_key")

STATUSES = (
    "reviewing",
    "released",
    "redacted_sent",
    "needs_human",
    "denied",
    "approved",
)

# Sender: takes the gated text to forward, returns the raw upstream
# answer. Injected so tests never touch the network.
Sender = Callable[[str], str]


@dataclass
class Hold:
    """One held send waiting on (or finished with) Glimmer review."""

    hold_id: str  # == the gate decision_id, used as review_url id
    text: str
    messages: list[dict[str, Any]]
    provider: str | None
    spans: list[Span]
    proven_ids: list[str] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    status: str = "reviewing"
    judgements: list[dict[str, Any] | None] = field(default_factory=list)
    confirmed: list[Span] = field(default_factory=list)
    answer: str | None = None
    sent_text: str | None = None
    redacted_preview: str = ""
    user_message: str = "Held for review: possible pointer to sensitive data."
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    outcome_log: list[str] = field(default_factory=list)


class HoldStore:
    """Thread-safe in-memory hold registry."""

    def __init__(self) -> None:
        self._holds: dict[str, Hold] = {}
        self._lock = threading.Lock()

    def put(self, hold: Hold) -> Hold:
        with self._lock:
            self._holds[hold.hold_id] = hold
        return hold

    def get(self, hold_id: str) -> Hold | None:
        with self._lock:
            return self._holds.get(hold_id)

    def clear(self) -> None:
        with self._lock:
            self._holds.clear()


HOLDS = HoldStore()


def _default_policy() -> Policy:
    return load_policy()


def _default_registry() -> CanaryRegistry:
    return CanaryRegistry(get_settings().canary_seed, REGISTRY_KINDS)


def _proven_spans(hold: Hold) -> list[Span]:
    wanted = set(hold.proven_ids)
    return [s for s in hold.spans if s.span_id in wanted]


def _log_outcome(hold: Hold, outcome: str) -> None:
    """Append one review outcome line with a timestamp."""
    hold.outcome_log.append(f"{time.time():.3f} {outcome}")
    hold.updated_at = time.time()


def _log_decision(decision: Decision, text: str) -> None:
    """Append one review outcome to the hash-chained log; never fail."""
    try:
        from . import ecs_log

        ecs_log.append_decision(get_settings().log_path, decision, text)
    except Exception:
        pass


def _hold_decision(
    hold: Hold, decision: str, resolved_tier: int, reasons: list[str]
) -> Decision:
    return Decision(
        decision=decision,
        decision_id=uuid.uuid4().hex,
        spans=hold.spans,
        sent_text=hold.sent_text,
        placeholders={},
        resolved_tier=resolved_tier,
        latency_ms={},
        reasons=reasons,
        status="complete",
    )


def create_hold(
    decision: Decision,
    text: str,
    messages: list[dict[str, Any]],
    provider: str | None,
    *,
    needs_human_why: str | None = None,
) -> Hold:
    """Register one gate HOLD for background review; id == decision_id."""
    from .redact import redact as _redact

    proven = [s for s in decision.spans if s.status == "proven"]
    ambiguous = [s for s in decision.spans if s.status == "ambiguous"]
    pending = [
        {
            "span_id": s.span_id,
            "cp_start": s.cp_start,
            "cp_end": s.cp_end,
            "text": text[s.cp_start:s.cp_end],
        }
        for s in ambiguous
    ]
    if proven:
        preview, _ = _redact(text, proven)
    else:
        preview = text
    hold = Hold(
        hold_id=decision.decision_id,
        text=text,
        messages=[m for m in messages if isinstance(m, dict)],
        provider=provider,
        spans=list(decision.spans),
        proven_ids=[s.span_id for s in proven],
        pending=pending,
        evidence={
            "proven_by_code": [s.span_id for s in proven],
            "pending": [item["text"] for item in pending],
        },
        status="reviewing" if pending else "needs_human",
        redacted_preview=preview,
    )
    if needs_human_why is not None:
        hold.evidence["model_suspicion_only"] = [
            {
                "span": "",
                "judgement": None,
                "why_unverified": needs_human_why,
            }
        ]
    HOLDS.put(hold)
    return hold


def _benign_clear(judgement: Any) -> bool:
    """Model-benign only counts when confident and low-sensitivity."""
    if isinstance(judgement, dict):
        cls = judgement.get("cls")
        confidence = judgement.get("confidence", 0.0)
        sensitivity = judgement.get("sensitivity", 1.0)
    else:
        cls = getattr(judgement, "cls", None)
        confidence = getattr(judgement, "confidence", 0.0)
        sensitivity = getattr(judgement, "sensitivity", 1.0)
    try:
        return (
            cls == "benign"
            and float(confidence) >= 0.8
            and float(sensitivity) < 0.3
        )
    except (TypeError, ValueError):
        return False


def _suspicion(span_text: str, judgement: Any, why: str) -> dict[str, Any]:
    return {
        "span": span_text,
        "judgement": _judgement_dump(judgement),
        "why_unverified": why,
    }


def run_review(
    hold_id: str,
    *,
    judge_transport: Any = None,
    sender: Sender | None = None,
    policy: Policy | None = None,
    registry: CanaryRegistry | None = None,
) -> Hold | None:
    """Review one hold synchronously: judge + verify each pending span.

    Thread-safe entry point used directly by tests (with a
    ``MockTransport`` judge and a stub sender) and via
    :func:`start_review` in production.
    """
    hold = HOLDS.get(hold_id)
    if hold is None or hold.status != "reviewing":
        return hold
    policy = policy if policy is not None else _default_policy()
    registry = registry if registry is not None else _default_registry()
    text = hold.text
    tier1_spans = list(hold.spans)
    model = get_settings().judge_model

    proven = _proven_spans(hold)
    if any(s.subclass == "canary" for s in proven):
        hold.status = "needs_human"
        hold.evidence["model_suspicion_only"] = [
            _suspicion("", None, "tier1 proven canary is never auto-sent")
        ]
        _log_outcome(hold, "needs_human: proven canary")
        _log_decision(
            _hold_decision(hold, "HOLD", 4, ["proven canary -> needs_human"]),
            text,
        )
        return hold

    suspicions: list[dict[str, Any]] = []
    confirmed: list[Span] = []
    cleared = 0
    for item in list(hold.pending):
        span_text = item["text"]
        window = judge_module.make_window(
            text, item["cp_start"], item["cp_end"]
        )
        try:
            judgement = judge_module.judge(
                window, model, live=False, transport=judge_transport
            )
        except Exception:
            judgement = None
        if judgement is None:
            suspicions.append(
                _suspicion(span_text, None, "judge unavailable")
            )
            continue
        hold.judgements.append(_judgement_dump(judgement))
        try:
            verdict = verifier_module.verify(
                text, judgement, tier1_spans, policy, registry
            )
        except Exception as exc:
            suspicions.append(
                _suspicion(
                    span_text, judgement,
                    f"verification failed: {type(exc).__name__}",
                )
            )
            continue
        if verdict.confirmed and verdict.span is not None:
            if verdict.span.subclass == "canary":
                # Recorded for redaction, never for auto-send.
                confirmed.append(verdict.span)
                suspicions.append(
                    _suspicion(
                        span_text, judgement,
                        "model-confirmed canary is never auto-sent",
                    )
                )
                continue
            confirmed.append(verdict.span)
            continue
        if _benign_clear(judgement):
            cleared += 1
            continue
        suspicions.append(
            _suspicion(span_text, judgement, verdict.why)
        )

    if suspicions:
        hold.confirmed = confirmed
        hold.status = "needs_human"
        hold.evidence["model_suspicion_only"] = suspicions
        hold.evidence["pending"] = []
        _log_outcome(hold, f"needs_human: {len(suspicions)} unverified")
        _log_decision(
            _hold_decision(
                hold, "HOLD", 4,
                [f"{len(suspicions)} span(s) need human review"],
            ),
            text,
        )
        return hold

    if hold.status != "reviewing":
        # A human ruled while the review was running: keep their call.
        _log_outcome(hold, "aborted: human ruled during review")
        return hold

    if confirmed:
        return _auto_redact_send(hold, proven, confirmed, sender, text)
    return _auto_release(hold, proven, sender, text)


def _send_or_hold(
    hold: Hold, sent_text: str, mapping: dict[str, str],
    sender: Sender | None, text: str, kind: str,
) -> Hold:
    """Forward gated text; upstream failure holds for a human."""
    if sender is None:
        hold.status = "needs_human"
        hold.evidence["model_suspicion_only"] = [
            _suspicion("", None, "no sender: auto-send unavailable")
        ]
        _log_outcome(hold, "needs_human: no sender")
        return hold
    try:
        raw_answer = sender(sent_text)
    except Exception as exc:
        hold.status = "needs_human"
        hold.evidence["model_suspicion_only"] = [
            _suspicion(
                "", None,
                f"auto-send failed ({type(exc).__name__}): nothing was sent",
            )
        ]
        _log_outcome(hold, f"needs_human: auto-send failed ({type(exc).__name__})")
        return hold
    rehydrated, _, _ = rehydrate(raw_answer, mapping)
    hold.sent_text = sent_text
    hold.answer = rehydrated
    hold.status = kind
    hold.evidence["pending"] = []
    if mapping:
        parts = describe_placeholders(mapping)
        hold.user_message = (
            f"Sent with {len(mapping)} "
            f"{'redaction' if len(mapping) == 1 else 'redactions'}: "
            + ", ".join(parts) + "."
        )
    else:
        hold.user_message = "Sent clean"
    _log_outcome(hold, f"{kind}: auto-sent ({len(mapping)} redactions)")
    return hold


def _auto_redact_send(
    hold: Hold, proven: list[Span], confirmed: list[Span],
    sender: Sender | None, text: str,
) -> Hold:
    """All pending spans code-confirmed: redact and auto-send."""
    from .redact import redact as _redact

    combined = merge_overlaps(proven + confirmed)
    sent_text, mapping = _redact(text, combined)
    decision_id = uuid.uuid4().hex
    save_mapping(decision_id, mapping)
    hold.confirmed = confirmed
    result = _send_or_hold(hold, sent_text, mapping, sender, text, "redacted_sent")
    if result.status == "redacted_sent":
        _log_decision(
            _hold_decision(
                hold, "REDACT", 3,
                [f"{len(combined)} span(s) confirmed and redacted -> auto-send"],
            ),
            text,
        )
    return result


def _auto_release(
    hold: Hold, proven: list[Span], sender: Sender | None, text: str,
) -> Hold:
    """Every pending span model-benign and confident: release and send.

    Tier 1 proven spans stay redacted (a model answer never removes
    them); a Tier 1 proven secret blocks the release entirely.
    """
    from .redact import redact as _redact

    if any(s.cls == "secret" for s in proven):
        hold.status = "needs_human"
        hold.evidence["model_suspicion_only"] = [
            _suspicion("", None, "tier1 proven secret needs a human ruling")
        ]
        _log_outcome(hold, "needs_human: proven secret")
        _log_decision(
            _hold_decision(hold, "HOLD", 4, ["proven secret -> needs_human"]),
            text,
        )
        return hold
    if proven:
        sent_text, mapping = _redact(text, proven)
        decision_id = uuid.uuid4().hex
        save_mapping(decision_id, mapping)
    else:
        sent_text, mapping = text, {}
    result = _send_or_hold(hold, sent_text, mapping, sender, text, "released")
    if result.status == "released":
        _log_decision(
            _hold_decision(
                hold, "ALLOW" if not mapping else "REDACT", 3,
                ["model cleared every ambiguous span -> release"],
            ),
            text,
        )
    return result


def start_review(hold_id: str, **kwargs: Any) -> "threading.Thread":
    """Review one hold in a background daemon thread."""
    thread = threading.Thread(
        target=run_review,
        args=(hold_id,),
        kwargs=kwargs,
        name=f"sf-hold-review-{hold_id[:8]}",
        daemon=True,
    )
    thread.start()
    return thread


def hold_card(hold: Hold) -> dict[str, Any]:
    """Serialise one hold as the GET /api/holds/{id} evidence card."""
    proven_by_code = []
    for span in hold.spans:
        if span.span_id in set(hold.proven_ids):
            proven_by_code.append(
                {
                    "span_id": span.span_id,
                    "text": hold.text[span.cp_start:span.cp_end],
                    "cls": span.cls,
                    "subclass": span.subclass,
                }
            )
    return {
        "hold_id": hold.hold_id,
        "status": hold.status,
        "proven_by_code": proven_by_code,
        "model_suspicion_only": list(
            hold.evidence.get("model_suspicion_only", [])
        ),
        "pending": [item["text"] for item in hold.pending]
        if hold.status == "reviewing"
        else [],
        "redacted_preview": hold.redacted_preview,
        "answer": hold.answer,
        "sent_text": hold.sent_text,
        "user_message": hold.user_message,
    }


def deny_hold(hold: Hold, reason: str | None = None) -> Hold | None:
    """Deny a hold: send nothing. Only from reviewing/needs_human."""
    if hold.status not in ("reviewing", "needs_human"):
        return None
    hold.status = "denied"
    hold.user_message = "Denied: nothing was sent."
    _log_outcome(hold, f"denied by human{f': {reason}' if reason else ''}")
    _log_decision(
        _hold_decision(hold, "HOLD", 4, ["human denied -> nothing sent"]),
        hold.text,
    )
    return hold


def approve_hold(
    hold: Hold, *, sender: Sender,
) -> dict[str, Any] | None:
    """Approve a hold: forward the redacted text only.

    Proven and code-confirmed spans stay redacted; returns the pieces
    the route needs to build the /api/send shape, or None when the
    hold is already resolved.
    """
    if hold.status not in ("reviewing", "needs_human"):
        return None
    from .redact import redact as _redact

    combined = merge_overlaps(_proven_spans(hold) + list(hold.confirmed))
    if combined:
        sent_text, mapping = _redact(hold.text, combined)
    else:
        sent_text, mapping = hold.text, {}
    raw_answer = sender(sent_text)
    decision_id = uuid.uuid4().hex
    save_mapping(decision_id, mapping)
    rehydrated, highlights, unmatched = rehydrate(raw_answer, mapping)
    hold.sent_text = sent_text
    hold.answer = rehydrated
    hold.status = "approved"
    if mapping:
        parts = describe_placeholders(mapping)
        hold.user_message = (
            f"Sent with {len(mapping)} "
            f"{'redaction' if len(mapping) == 1 else 'redactions'}: "
            + ", ".join(parts) + "."
        )
    else:
        hold.user_message = "Sent clean"
    _log_outcome(hold, f"approved by human ({len(mapping)} redactions)")
    _log_decision(
        _hold_decision(
            hold, "ALLOW" if not mapping else "REDACT", 4,
            ["human approved redacted text -> send"],
        ),
        hold.text,
    )
    return {
        "sent_text": sent_text,
        "mapping_id": decision_id,
        "placeholders": dict(mapping),
        "answer": rehydrated,
        "highlights": highlights,
        "unmatched": unmatched,
    }


def reset_for_tests() -> None:
    """Drop every hold (tests only)."""
    HOLDS.clear()
