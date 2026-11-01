"""Asynchronous Glimmer review for HOLDs (T10).

``engine.gate(mode="send")`` HOLDS instantly for flagged messages, then
:func:`start_review` reviews them in a
background thread. The pending whole-message item is judged once with
Glimmer and each pending span is judged per-span; every answer is
verified in code (Tier 3):

- a Glimmer non-benign whole-message judgement with a span is
  verified: confirmed -> redact that span too and auto-send
  (``redacted_sent``); unconfirmed -> ``needs_human`` with evidence
  showing Glimmer's class, confidence and span;
- a Glimmer benign whole-message judgement with confidence >= 0.8
  and sensitivity < 0.3 releases (send with Tier 1 redactions only);
- judge None -> ``needs_human``;
- ambiguous Tier 1 spans keep the per-span flow: confirmed spans
  redact and auto-send, benign-confident spans clear, anything else
  -> ``needs_human`` with ``model_suspicion_only`` entries.

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
from .redact import PLACEHOLDER_RE, rehydrate, save_mapping
from .spans import Span, merge_overlaps, span_from_cp_range

from shadowfenced.canary import DEFAULT_KINDS as REGISTRY_KINDS

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


def _log_decision(
    decision: Decision, text: str, log_path: str | None = None,
) -> None:
    """Append one review outcome to the hash-chained log; never fail.

    ``log_path`` pins the file the review was spawned for. Callers that
    outlive their spawning context (background review threads) must pass
    the path captured at spawn: resolving the configured path lazily at
    write time would let a late write land in another context's log.
    """
    try:
        from . import ecs_log

        ecs_log.append_decision(
            log_path if log_path is not None else get_settings().log_path,
            decision,
            text,
        )
    except Exception:
        pass


def _hold_decision(
    hold: Hold, decision: str, resolved_tier: int, reasons: list[str]
) -> Decision:
    # Reuse the hold id (== the original gate decision id) so the stats
    # snapshot counts one decision per decision_id with the latest outcome
    # winning, instead of counting the HOLD and its resolution twice.
    return Decision(
        decision=decision,
        decision_id=hold.hold_id,
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

    from .engine import MESSAGE_PENDING_CHARS, has_sensitivity_cue

    proven = [s for s in decision.spans if s.status == "proven"]
    ambiguous = [s for s in decision.spans if s.status == "ambiguous"]
    pending: list[dict[str, Any]] = []
    message_text: str | None = None
    evidence_pending = (decision.evidence or {}).get("pending")
    if isinstance(evidence_pending, list):
        for entry in evidence_pending:
            if (
                isinstance(entry, dict)
                and entry.get("kind") == "message"
                and isinstance(entry.get("text"), str)
            ):
                message_text = entry["text"][:MESSAGE_PENDING_CHARS]
                break
    if message_text is None and needs_human_why is None:
        if ambiguous or has_sensitivity_cue(text):
            message_text = text[:MESSAGE_PENDING_CHARS]
    if message_text is not None:
        pending.append({"kind": "message", "text": message_text})
    pending.extend(
        {
            "kind": "span",
            "span_id": s.span_id,
            "cp_start": s.cp_start,
            "cp_end": s.cp_end,
            "text": text[s.cp_start:s.cp_end],
            "cls": s.cls,
        }
        for s in ambiguous
    )
    if proven:
        preview, _ = _redact(text, proven)
    else:
        preview = text
    hold_evidence: dict[str, object] = {
        "proven_by_code": [s.span_id for s in proven],
        "pending": [item["text"] for item in pending],
    }
    for _key in ("reid", "person_search", "doc_type", "doc_warning",
                 "doc_signals"):
        if isinstance(decision.evidence, dict) and _key in decision.evidence:
            hold_evidence[_key] = decision.evidence[_key]
    hold = Hold(
        hold_id=decision.decision_id,
        text=text,
        messages=[m for m in messages if isinstance(m, dict)],
        provider=provider,
        spans=list(decision.spans),
        proven_ids=[s.span_id for s in proven],
        pending=pending,
        evidence=hold_evidence,
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


def _suspicion(
    span_text: str,
    judgement: Any,
    why: str,
    *,
    cp_start: int | None = None,
    cp_end: int | None = None,
    cls: str | None = None,
) -> dict[str, Any]:
    """One model-suspicion entry for the evidence card.

    Code-point offsets locate the span in ``hold.text`` so a human
    approve can replace it with a placeholder; ``cls`` names the
    suspected class for the placeholder label and the card title.
    """
    if isinstance(judgement, dict):
        judged = judgement.get("cls")
    else:
        judged = getattr(judgement, "cls", None)
    # The model's suspected class wins; the Tier 1 class is the fallback.
    if not (isinstance(judged, str) and judged):
        judged = cls
    cls = judged
    return {
        "span": span_text,
        "judgement": _judgement_dump(judgement),
        "why_unverified": why,
        "cp_start": cp_start,
        "cp_end": cp_end,
        "cls": cls if isinstance(cls, str) and cls else None,
    }


def _judgement_summary(judgement: Any) -> tuple[str, float, str]:
    """Return (cls, confidence, span) for a dict or Judgement verdict."""
    if isinstance(judgement, dict):
        cls = judgement.get("cls")
        confidence = judgement.get("confidence", 0.0)
        span_text = judgement.get("span", "") or ""
    else:
        cls = getattr(judgement, "cls", None)
        confidence = getattr(judgement, "confidence", 0.0)
        span_text = getattr(judgement, "span", "") or ""
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.0
    if not isinstance(cls, str) or not cls:
        cls = "unknown"
    if not isinstance(span_text, str):
        span_text = ""
    return cls, confidence_value, span_text


def _locate(text: str, span_text: str) -> tuple[int | None, int | None]:
    """Code-point offsets of a span inside the text, if present."""
    if span_text and span_text in text:
        start = text.find(span_text)
        return start, start + len(span_text)
    return None, None


#: Max message chars judged in background review: the front decides it.
MESSAGE_JUDGE_CHARS = 1000


def _judge_for_review(
    hold: Hold,
    window: str,
    model: str,
    judge_transport: Any,
    meta_transport: Any,
) -> Any:
    """Judge one window; on the hosted fallback record the trace label.

    Never raises: any failure returns None so the review fails closed.
    """
    try:
        from . import hosted as hosted_module

        judgement, label = hosted_module.judge_for_review(
            window,
            model,
            judge_transport=judge_transport,
            meta_transport=meta_transport,
        )
    except Exception:
        return None
    if label:
        hold.evidence["judge"] = label
        trace = hold.evidence.setdefault("trace", [])
        if isinstance(trace, list):
            trace.append(
                {
                    "step": "tier2.judge",
                    "tool": "judge",
                    "detail": label,
                    "result": "judged" if judgement is not None else "unavailable",
                }
            )
    return judgement


def _review_message_item(
    hold: Hold,
    item: dict[str, Any],
    text: str,
    tier1_spans: list[Span],
    model: str,
    judge_transport: Any,
    policy: Policy,
    registry: CanaryRegistry | None,
    suspicions: list[dict[str, Any]],
    confirmed: list[Span],
    meta_transport: Any = None,
) -> None:
    """Judge the whole message once; verify a non-benign span in code.

    Benign with confidence >= 0.8 and sensitivity < 0.3 clears the
    message. Non-benign with a span is verified as today: confirmed
    redacts that span too on auto-send, unconfirmed needs a human
    with evidence showing Glimmer's class, confidence and span.
    Judge None (or anything else unprovable) needs a human.
    """
    message_text = item.get("text", "")
    if not isinstance(message_text, str):
        message_text = ""
    judgement = _judge_for_review(
        hold, message_text[:MESSAGE_JUDGE_CHARS], model, judge_transport, meta_transport
    )
    if judgement is None:
        suspicions.append(
            _suspicion("", None, "Glimmer unavailable")
        )
        return
    hold.judgements.append(_judgement_dump(judgement))
    if _benign_clear(judgement):
        return
    cls, confidence, span_text = _judgement_summary(judgement)
    start, end = _locate(text, span_text)
    # The evidence entry carries the flagged span (or "" when there is
    # nothing to locate), never the whole message: the UI highlights
    # the flagged part in the redacted preview.
    flagged = span_text if start is not None else ""
    if cls != "benign" and span_text:
        try:
            verdict = verifier_module.verify(
                text, judgement, tier1_spans, policy, registry
            )
        except Exception as exc:
            suspicions.append(
                _suspicion(
                    flagged, judgement,
                    f"Glimmer flagged {cls} (confidence {confidence:.2f}, "
                    f"span {span_text!r}) but verification failed: "
                    f"{type(exc).__name__}",
                    cp_start=start, cp_end=end,
                )
            )
            return
        if verdict.confirmed and verdict.span is not None:
            if verdict.span.subclass == "canary":
                # Recorded for redaction, never for auto-send.
                confirmed.append(verdict.span)
                suspicions.append(
                    _suspicion(
                        flagged, judgement,
                        "model-confirmed canary is never auto-sent",
                        cp_start=start, cp_end=end,
                    )
                )
                return
            confirmed.append(verdict.span)
            return
        suspicions.append(
            _suspicion(
                flagged, judgement,
                f"Glimmer flagged {cls} (confidence {confidence:.2f}, "
                f"span {span_text!r}): {verdict.why}",
                cp_start=start, cp_end=end,
            )
        )
        return
    suspicions.append(
        _suspicion(
            flagged, judgement,
            f"Glimmer flagged {cls} (confidence {confidence:.2f}) "
            "without a verifiable span",
            cp_start=start, cp_end=end,
        )
    )


def run_review(
    hold_id: str,
    *,
    judge_transport: Any = None,
    meta_transport: Any = None,
    sender: Sender | None = None,
    policy: Policy | None = None,
    registry: CanaryRegistry | None = None,
    log_path: str | None = None,
) -> Hold | None:
    """Review one hold synchronously: judge the message once, verify spans.

    Thread-safe entry point used directly by tests (with a
    ``MockTransport`` judge and a stub sender) and via
    :func:`start_review` in production. ``log_path`` pins the
    decision-log file so background reviews log to the file their
    own context configured, even if they finish late.
    """
    hold = HOLDS.get(hold_id)
    if hold is None or hold.status != "reviewing":
        return hold
    if log_path is None:
        log_path = get_settings().log_path
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
            log_path,
        )
        return hold

    suspicions: list[dict[str, Any]] = []
    confirmed: list[Span] = []
    cleared = 0
    for item in list(hold.pending):
        if item.get("kind", "span") == "message":
            _review_message_item(
                hold, item, text, tier1_spans, model, judge_transport,
                policy, registry, suspicions, confirmed,
                meta_transport=meta_transport,
            )
            continue
        span_text = item["text"]
        window = judge_module.make_window(
            text, item["cp_start"], item["cp_end"]
        )
        judgement = _judge_for_review(
            hold, window, model, judge_transport, meta_transport
        )
        if judgement is None:
            suspicions.append(
                _suspicion(
                    span_text, None, "judge unavailable",
                    cp_start=item["cp_start"], cp_end=item["cp_end"],
                    cls=item.get("cls"),
                )
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
                    cp_start=item["cp_start"], cp_end=item["cp_end"],
                    cls=item.get("cls"),
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
                        cp_start=item["cp_start"], cp_end=item["cp_end"],
                        cls=item.get("cls"),
                    )
                )
                continue
            confirmed.append(verdict.span)
            continue
        if _benign_clear(judgement):
            cleared += 1
            continue
        suspicions.append(
            _suspicion(
                span_text, judgement, verdict.why,
                cp_start=item["cp_start"], cp_end=item["cp_end"],
                cls=item.get("cls"),
            )
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
            log_path,
        )
        return hold

    if hold.status != "reviewing":
        # A human ruled while the review was running: keep their call.
        _log_outcome(hold, "aborted: human ruled during review")
        return hold

    if confirmed:
        return _auto_redact_send(
            hold, proven, confirmed, sender, text, log_path,
        )
    return _auto_release(hold, proven, sender, text, log_path)


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
    sender: Sender | None, text: str, log_path: str | None = None,
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
            log_path,
        )
    return result


def _auto_release(
    hold: Hold, proven: list[Span], sender: Sender | None, text: str,
    log_path: str | None = None,
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
            log_path,
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
            log_path,
        )
    return result


def start_review(
    hold_id: str, log_path: str | None = None, **kwargs: Any
) -> "threading.Thread":
    """Review one hold in a background daemon thread.

    ``log_path`` pins the decision-log file captured by the spawner so
    a late write from this thread cannot land in another context's log.
    """
    thread = threading.Thread(
        target=run_review,
        args=(hold_id,),
        kwargs={**kwargs, "log_path": log_path},
        name=f"sf-hold-review-{hold_id[:8]}",
        daemon=True,
    )
    thread.start()
    return thread


def hold_title(hold: Hold) -> str:
    """One user-facing card title for the hold's current outcome."""
    if hold.status == "needs_human":
        judged = [
            e for e in hold.evidence.get("model_suspicion_only", [])
            if e.get("judgement")
        ]
        if judged:
            first = judged[0]
            cls = (first.get("cls") or "issue") if isinstance(first, dict) else "issue"
            confidence = (first.get("judgement") or {}).get("confidence")
            try:
                pct = f"{int(round(float(confidence) * 100))}%"
            except (TypeError, ValueError):
                pct = "unknown confidence"
            return (
                f"Glimmer flagged a possible {cls} ({pct}). "
                "Code could not prove it, so it's your call."
            )
        return (
            "Glimmer flagged a possible issue. "
            "Code could not prove it, so it's your call."
        )
    if hold.status == "redacted_sent":
        return "Glimmer found it and code confirmed it. Sent with the part removed."
    if hold.status == "released":
        return "Glimmer checked it: nothing sensitive. Sent."
    if hold.status == "reviewing":
        return "Glimmer is reviewing the flagged part."
    if hold.status == "denied":
        return "Denied: nothing was sent."
    return hold.user_message or hold.status


def _utf16_len(text: str) -> int:
    """Length of text in UTF-16 code units (the /api/scan offset unit)."""
    return len(text.encode("utf-16-le")) // 2


def preview_marks(hold: Hold) -> list[dict[str, Any]]:
    """UTF-16 ranges into ``hold.redacted_preview`` for highlighting.

    Proven spans surface as their ``[LABEL_N]`` placeholders (kind
    ``proven``); located model-suspicion spans surface as raw text
    (kind ``suspicion``) so the UI can colour them differently.
    Offsets are UTF-16 code units, matching the scan contract and plain
    JS string slicing.
    """
    preview = hold.redacted_preview or ""
    marks: list[dict[str, Any]] = []
    for match in PLACEHOLDER_RE.finditer(preview):
        marks.append(
            {
                "start": _utf16_len(preview[:match.start()]),
                "end": _utf16_len(preview[:match.end()]),
                "kind": "proven",
            }
        )
    occupied = [(m["start"], m["end"]) for m in marks]
    for entry in hold.evidence.get("model_suspicion_only", []):
        text = entry.get("span") or ""
        if not text:
            continue
        size = _utf16_len(text)
        at = preview.find(text)
        while at >= 0 and any(
            _utf16_len(preview[:at]) < end
            and start < _utf16_len(preview[:at]) + size
            for start, end in occupied
        ):
            at = preview.find(text, at + 1)
        if at < 0:
            continue
        start16 = _utf16_len(preview[:at])
        marks.append(
            {"start": start16, "end": start16 + size, "kind": "suspicion"}
        )
        occupied.append((start16, start16 + size))
    marks.sort(key=lambda m: (m["start"], m["end"]))
    return marks


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
        "provider": hold.provider,
        "judge": hold.evidence.get("judge"),
        "trace": list(hold.evidence.get("trace", []) or []),
        "title": hold_title(hold),
        "proven_by_code": proven_by_code,
        "model_suspicion_only": list(
            hold.evidence.get("model_suspicion_only", [])
        ),
        "pending": [item["text"] for item in hold.pending]
        if hold.status == "reviewing"
        else [],
        "redacted_preview": hold.redacted_preview,
        "preview_marks": preview_marks(hold),
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


def _suspicion_label(cls: str | None) -> tuple[str, str]:
    """Placeholder (class, subclass) for one model-suspicion span.

    Beacons label ``BEACON``; every other class labels per class so an
    approve replaces the flagged part without implying proof.
    """
    name = (cls or "beacon").strip().lower()
    if name == "secret":
        return ("secret", "suspicion")
    if name == "pii":
        return ("pii", "")
    if name == "business":
        return ("business", "business")
    if name == "beacon":
        return ("beacon", "cue")
    return (name, "suspicion")


def _suspicion_spans(hold: Hold) -> list[Span]:
    """Build redaction spans for each located model-suspicion entry."""
    spans: list[Span] = []
    for entry in hold.evidence.get("model_suspicion_only", []):
        start = entry.get("cp_start")
        end = entry.get("cp_end")
        if (
            not isinstance(start, int) or not isinstance(end, int)
            or start < 0 or end > len(hold.text) or start >= end
        ):
            continue
        cls, subclass = _suspicion_label(entry.get("cls"))
        confidence = 0.5
        judgement = entry.get("judgement") or {}
        try:
            confidence = float(judgement.get("confidence", 0.5))
        except (TypeError, ValueError, AttributeError):
            pass
        spans.append(
            span_from_cp_range(
                hold.text, start, end,
                cls=cls, subclass=subclass, tier=2, proof="model",
                status="suspicion",
                message=f"model suspicion: {cls}",
                why=str(entry.get("why_unverified") or "model suspicion"),
                suggested_action="review", can_keep=True,
                confidence=confidence,
            )
        )
    return spans


def approve_hold(
    hold: Hold, *, sender: Sender,
) -> dict[str, Any] | None:
    """Approve a hold: forward the text with the flagged part removed.

    Proven and code-confirmed spans stay redacted, and every located
    model-suspicion span is replaced with a placeholder too
    (``[BEACON_n]`` for beacons, per-class otherwise); returns the
    pieces the route needs to build the /api/send shape, or None when
    the hold is already resolved.
    """
    if hold.status not in ("reviewing", "needs_human"):
        return None
    from .redact import redact as _redact

    already = _proven_spans(hold) + list(hold.confirmed)
    covered = [(s.cp_start, s.cp_end) for s in already]
    extra = [
        s for s in _suspicion_spans(hold)
        if not any(s.cp_start < end and start < s.cp_end for start, end in covered)
    ]
    combined = merge_overlaps(already + extra)
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
