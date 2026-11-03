"""Tier-1-only gate: code findings in, a typed decision out.

Severity ordering is BLOCK > HOLD > REDACT > WARN > ALLOW. A proven canary
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

from . import reid as reid_module
from . import tier1
from .canary import CanaryRegistry
from .policy import Policy
from .redact import redact, save_mapping
from .spans import Span

DECISIONS = ("ALLOW", "REDACT", "BLOCK", "HOLD", "WARN")

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
    "CONTRACT_CLAUSE": "contract clause",
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
        ordered = sorted(
            decision.spans, key=lambda span: (span.cp_start, span.cp_end)
        )
        if not any(
            span.subclass == "canary" and span.status == "proven"
            for span in ordered
        ):
            for span in ordered:
                if (
                    span.subclass == "provider_key"
                    and span.status == "proven"
                    and span.message
                ):
                    return span.message
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
    else:
        person_banner = (decision.evidence or {}).get("person_search", {}).get("banner")
        reid_banner = person_banner or (decision.evidence or {}).get("reid", {}).get("banner")
        if decision.decision == "WARN":
            base = reid_banner or (
                "Possible re-identification risk: the combined details "
                "could identify someone. You may send anyway."
            )
        elif decision.decision == "HOLD":
            base = reid_banner or "Held for review: possible pointer to sensitive data."
        else:
            base = "Sent clean"
    if decision.decision == "BLOCK":
        _reid_block = (decision.evidence or {}).get("reid", {})
        if (isinstance(_reid_block, dict) and _reid_block.get("outcome") == "BLOCK"
                and _reid_block.get("banner")):
            base = str(_reid_block["banner"])
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
    rule_ids: list[str] = field(default_factory=list)  # catalogue ids that fired


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


def tier1_proves_identity_bundle(
    text: str,
    conversation: list[str] | None = None,
) -> bool:
    """True when Tier 1 code alone proves a full identity bundle.

    Code-only check over :func:`tier1.scan_reid` kinds with the same
    rule as T66 (:func:`reid.is_full_identity_bundle`): name + date
    of birth + (street or postcode) + (phone or email), in one
    message or across the conversation. Never calls a model.
    """
    try:
        kinds: set[str] = set()
        for item in tier1.scan_reid(text):
            kind = item.get("kind")
            if isinstance(kind, str) and kind:
                kinds.add(kind)
        for earlier in conversation or []:
            try:
                for item in tier1.scan_reid(earlier):
                    kind = item.get("kind")
                    if isinstance(kind, str) and kind:
                        kinds.add(kind)
            except Exception:
                continue
        return reid_module.is_full_identity_bundle(kinds)
    except Exception:
        return False


def has_proven_secret_block(spans: list[Span]) -> bool:
    """True when Tier 1 proved a span that must BLOCK with no override.

    A proven span whose suggested action is ``block`` and which the user
    may not keep (secrets, canaries, protected keys) decides BLOCK on
    its own: precedence BLOCK > everything makes further model checks
    pointless, so the gate returns immediately without calling Glimmer
    Tier 2, person search, cue review or any contract/model check.
    """
    return any(
        span.status == "proven"
        and span.suggested_action == "block"
        and not span.can_keep
        for span in spans
    )


def _confidential_contract_marker(text: str) -> str | None:
    """Stamp name when a classified contract must hold for review.

    Returns the handling-caveat stamp (``confidential``,
    ``do-not-distribute`` or ``privileged``; fixed vocabulary, never
    raw text) when the document classifier says contract AND the
    text carries such a stamp, else None. The gate raises the
    decision to HOLD with the reason "Confidential contract text"
    so a human picks Redact-and-send; redaction itself still
    replaces every proven span with a placeholder.
    """
    try:
        from . import contract as contract_module
        from . import docclass as docclass_module

        info = docclass_module.classify(text)
    except Exception:
        return None
    if not isinstance(info, dict) or info.get("doc_type") != "contract":
        return None
    try:
        return contract_module.confidential_marker_of(text)
    except Exception:
        return None

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


def cue_word(text: str) -> str | None:
    """Name the sensitivity cue that fired, without echoing message text.

    Returns the matched cue word lowercased with internal whitespace
    collapsed (``"born"`` for the Ada Lovelace message), ``"unc path"``
    for a bare UNC server path, or None when nothing fired. Only this
    cue fragment may appear in a user-facing reason: never any other
    part of the message.
    """
    match = _SCREEN_CUE_RE.search(text)
    if match is not None:
        return re.sub(r"\s+", " ", match.group(0)).strip().lower()
    if _SCREEN_UNC_RE.search(text) is not None:
        return "unc path"
    return None


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


def _review_noun() -> str:
    try:
        from . import hosted as hosted_module

        if hosted_module.is_hosted():
            return "Muse Spark"
    except Exception:
        pass
    return "Glimmer"


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
            cue = cue_word(text)
            if cue is not None:
                reasons.append(
                    f"sensitivity cue word needs {_review_noun()} review (cue='{cue}') -> HOLD"
                )
            else:
                reasons.append(f"sensitivity cue word needs {_review_noun()} review -> HOLD")
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
        provider_hit = sorted(
            (
                span for span in enforced
                if span.subclass == "provider_key"
                and span.status == "proven"
            ),
            key=lambda span: (span.cp_start, span.cp_end),
        )
        if provider_hit:
            reasons.append(
                f"proven provider key -> BLOCK ({provider_hit[0].why})"
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
        contract_marker = _confidential_contract_marker(text)
        if contract_marker is not None:
            reasons.append(
                "Confidential contract text "
                f"({contract_marker} marker) -> HOLD"
            )
            if trace_out is not None:
                trace_out.append(
                    {"step": "contract.hold", "tool": "gate",
                     "detail": f"contract doc + {contract_marker} marker -> HOLD",
                     "result": "HOLD", "ms": 0.0}
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
                        "confidential_contract": True,
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


def _is_cue_only_hold(decision: Decision) -> bool:
    """True when a HOLD came only from a sensitivity cue (no ambiguous spans).

    A classified-contract HOLD is never cue-only: the handling-caveat
    stamp plus the contract classification require human review, so
    re-id or person-search noise must not downgrade them to WARN.
    """
    if decision.decision != "HOLD":
        return False
    if (decision.evidence or {}).get("confidential_contract"):
        return False
    return not any(s.status == "ambiguous" for s in decision.spans)


def _drop_business_name_overlaps(
    decision: Decision, current: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop re-id person-name findings over Tier 1 proven business spans.

    A proven codename or client name (e.g. an internal project name) is
    not a private person: counting the same characters as a ``name``
    quasi-identifier would turn routine business redaction into a
    re-identification hold. Other kinds and PII overlaps (holder names
    really are person names) are kept.
    """
    biz = [(s.cp_start, s.cp_end) for s in decision.spans
           if s.cls == "business" and s.status == "proven"]
    if not biz or not current:
        return current
    kept: list[dict[str, Any]] = []
    for item in current:
        if item.get("kind") != "name":
            kept.append(item)
            continue
        span = item.get("span", (0, 0))
        try:
            start, end = int(span[0]), int(span[1])
        except (TypeError, ValueError, IndexError):
            kept.append(item)
            continue
        if any(start < bend and bstart < end for bstart, bend in biz):
            continue
        kept.append(item)
    return kept


def _warn_from_hold(
    text: str,
    decision: Decision,
    evidence: dict[str, Any],
    reasons: list[str],
) -> Decision:
    """Build a WARN from a cue-only HOLD base (send mode user review)."""
    evidence = {k: v for k, v in (evidence or {}).items()
                if k != "pending"}
    proven = [s for s in decision.spans if s.status == "proven"]
    if proven:
        sent_text, mapping = redact(text, proven)
        decision_id = uuid.uuid4().hex
        save_mapping(decision_id, mapping)
        return Decision(
            decision="WARN",
            decision_id=decision_id,
            spans=decision.spans,
            sent_text=sent_text,
            placeholders=dict(mapping),
            resolved_tier=1,
            latency_ms=dict(decision.latency_ms),
            reasons=reasons,
            evidence=evidence,
        )
    return Decision(
        decision="WARN",
        decision_id=uuid.uuid4().hex,
        spans=decision.spans,
        sent_text=text,
        placeholders={},
        resolved_tier=1,
        latency_ms=dict(decision.latency_ms),
        reasons=reasons,
        evidence=evidence,
    )


def _apply_reid(
    text: str,
    decision: Decision,
    conversation: list[str] | None,
    residual: bool,
    judge_fn: Any,
    mode: str,
    trace_out: list[dict[str, Any]] | None,
    person_search_judge_fn: Any = None,
) -> Decision:
    """Layer re-identification scoring over a finished gate decision.

    Extraction runs through ``tier1.scan_reid`` (trace step
    ``reid.extract``); scoring appends ``reid.combine``. BLOCK and
    existing HOLD decisions are never downgraded: the re-id payload is
    attached to their evidence instead. Otherwise WARN returns sendable
    text with the banner, and HOLD sends nothing. The optional residual
    check runs in tier-1 mode only (the send path never calls a model
    inline) against the generalised text when offered, else the
    redacted-or-plain text. Evidence carries kinds, scores and
    fingerprints only: raw values stay out of traces and logs.
    """
    try:
        current = tier1.scan_reid(text, trace_out=trace_out)
        current = _drop_business_name_overlaps(decision, current)
    except Exception:
        current = []
        if trace_out is not None:
            trace_out.append(
                {"step": "reid.extract", "tool": "tier1",
                 "detail": "extraction failed", "result": "error",
                 "ms": 0.0}
            )
    try:
        result = reid_module.assess(
            text, conversation=conversation, current_findings=current
        )
    except Exception as exc:
        if decision.decision == "BLOCK":
            decision.reasons.append(
                f"reid error ({type(exc).__name__}); keeping BLOCK"
            )
            return _apply_person_search(
                text, decision, conversation, current,
                person_search_judge_fn, trace_out, mode,
            )
        reasons = list(decision.reasons) + [
            f"reid error ({type(exc).__name__}) -> HOLD"
        ]
        return Decision(
            decision="HOLD",
            decision_id=uuid.uuid4().hex,
            spans=decision.spans,
            sent_text=None,
            placeholders={},
            resolved_tier=1,
            latency_ms=dict(decision.latency_ms),
            reasons=reasons,
            evidence=dict(decision.evidence or {}),
        )
    reid_module.trace_combine(
        trace_out, result.score, result.combination, result.outcome
    )
    if result.outcome == "OK":
        return _apply_person_search(
            text, decision, conversation, current,
            person_search_judge_fn, trace_out,
        )
    evidence: dict[str, Any] = dict(decision.evidence or {})
    evidence["reid"] = {
        "score": result.score,
        "combination": list(result.combination),
        "outcome": result.outcome,
        "banner": result.banner,
        "generalised_text": result.generalised_text,
    }
    if result.outcome == "BLOCK":
        _block_line = "reid: score={} kinds={} -> BLOCK".format(
            result.score, "+".join(result.combination))
        _block_decision = Decision(
            decision="BLOCK",
            decision_id=uuid.uuid4().hex,
            spans=decision.spans,
            sent_text=None,
            placeholders={},
            resolved_tier=1,
            latency_ms=dict(decision.latency_ms),
            reasons=[*decision.reasons, _block_line,
                     "full identity bundle -> BLOCK"],
            evidence=evidence,
        )
        return _apply_person_search(
            text, _block_decision, conversation, current,
            person_search_judge_fn, trace_out, mode,
        )
    score_line = "reid: score={} kinds={} -> {}".format(
        result.score, "+".join(result.combination), result.outcome
    )
    if decision.decision == "BLOCK":
        decision.reasons.append(score_line)
        decision.evidence.update(evidence)
        return _apply_person_search(
            text, decision, conversation, current,
            person_search_judge_fn, trace_out, mode,
        )
    if decision.decision == "HOLD":
        if mode == "send" and _is_cue_only_hold(decision):
            reasons = list(decision.reasons) + [
                score_line,
                "specific re-id finding replaces generic cue hold -> WARN",
            ]
            warn_decision = _warn_from_hold(text, decision, evidence, reasons)
            return _apply_person_search(
                text, warn_decision, conversation, current,
                person_search_judge_fn, trace_out, mode,
            )
        decision.reasons.append(score_line)
        decision.evidence.update(evidence)
        return _apply_person_search(
            text, decision, conversation, current,
            person_search_judge_fn, trace_out, mode,
        )
    if residual and mode != "send":
        candidate = result.generalised_text or decision.sent_text or text
        residual_result = reid_module.residual_check(candidate, judge_fn)
        reid_module.trace_residual(trace_out, residual_result)
        if residual_result.hold:
            reasons = list(decision.reasons) + [
                score_line,
                "reid residual clues still identify or locate -> HOLD",
            ]
            residual_payload = dict(evidence["reid"])
            residual_payload["residual"] = {
                "verified": len(residual_result.verified_clues),
                "fingerprints": sorted(
                    {reid_module.fingerprint(c)
                     for c in residual_result.verified_clues}
                ),
                "can_identify": residual_result.can_identify,
                "can_locate": residual_result.can_locate,
            }
            evidence["reid"] = residual_payload
            return Decision(
                decision="HOLD",
                decision_id=uuid.uuid4().hex,
                spans=decision.spans,
                sent_text=None,
                placeholders={},
                resolved_tier=1,
                latency_ms=dict(decision.latency_ms),
                reasons=reasons,
                evidence=evidence,
            )
    elif residual:
        decision.reasons.append("reid residual check deferred in send mode")
    if result.outcome == "HOLD":
        if mode == "send":
            reasons = list(decision.reasons) + [
                score_line,
                "reid combination needs user review -> WARN",
            ]
            if decision.decision == "REDACT":
                decision.reasons.extend(reasons[len(decision.reasons):])
                decision.evidence.update(evidence)
                return _apply_person_search(
                    text, decision, conversation, current,
                    person_search_judge_fn, trace_out, mode,
                )
            warn_decision = Decision(
                decision="WARN",
                decision_id=uuid.uuid4().hex,
                spans=decision.spans,
                sent_text=(decision.sent_text
                           if decision.decision == "REDACT" else text),
                placeholders=dict(decision.placeholders or {}),
                resolved_tier=1,
                latency_ms=dict(decision.latency_ms),
                reasons=reasons,
                evidence=evidence,
            )
            return _apply_person_search(
                text, warn_decision, conversation, current,
                person_search_judge_fn, trace_out, mode,
            )
        reasons = list(decision.reasons) + [
            score_line,
            "reid combination can identify or locate -> HOLD",
        ]
        hold_decision = Decision(
            decision="HOLD",
            decision_id=uuid.uuid4().hex,
            spans=decision.spans,
            sent_text=None,
            placeholders={},
            resolved_tier=1,
            latency_ms=dict(decision.latency_ms),
            reasons=reasons,
            evidence=evidence,
        )
        return _apply_person_search(
            text, hold_decision, conversation, current,
            person_search_judge_fn, trace_out, mode,
        )
    reasons = list(decision.reasons) + [
        score_line,
        "reid combination needs user review -> WARN",
    ]
    if decision.decision == "REDACT":
        # REDACT outranks WARN: keep the redaction with re-id evidence.
        decision.reasons.extend(reasons[len(decision.reasons):])
        decision.evidence.update(evidence)
        return _apply_person_search(
            text, decision, conversation, current,
            person_search_judge_fn, trace_out, mode,
        )
    warn_decision = Decision(
        decision="WARN",
        decision_id=uuid.uuid4().hex,
        spans=decision.spans,
        sent_text=text,
        placeholders={},
        resolved_tier=1,
        latency_ms=dict(decision.latency_ms),
        reasons=reasons,
        evidence=evidence,
    )
    return _apply_person_search(
        text, warn_decision, conversation, current,
        person_search_judge_fn, trace_out, mode,
    )


def _downgrade_verified_public_figure(
    text: str,
    decision: Decision,
    conversation: list[str] | None,
    current: list[dict[str, Any]] | None,
    result: Any,
    trace_out: list[dict[str, Any]] | None,
) -> Decision | None:
    """ALLOW when Glimmer verifies a public figure in encyclopedic framing.

    Tier 3 coordination for T48 rule 2, extended in T53 to pure cue-word
    HOLDs with no re-id case: the layer may still warn on a published
    birthday plus a name, but when Glimmer has
    verified (exact-substring subject, confidence >= 0.6) that the
    subject is a public figure, the question uses encyclopedic framing
    and no current contact detail or ID number is present, there is no
    private person to protect. Never touches BLOCK or REDACT, and never
    downgrades while Tier 1 review is still pending (ambiguous spans).
    """
    if decision.decision not in ("WARN", "HOLD"):
        return None
    reid_info = (decision.evidence or {}).get("reid")
    if reid_info is not None and reid_info.get("outcome") not in ("WARN", "HOLD"):
        return None
    if reid_info is None:
        if not decision.reasons or not all(
            "sensitivity cue word" in reason for reason in decision.reasons
        ):
            return None
        if any(getattr(span, "proof", "") == "custom" for span in decision.spans):
            return None
    if result.warn or not result.triggered:
        return None
    if result.subject_public is not True or not result.verified_subject:
        return None
    try:
        confidence = float(result.confidence)
    except (TypeError, ValueError):
        return None
    if confidence < 0.6:
        return None
    if any(getattr(span, "status", "") == "ambiguous"
           for span in decision.spans):
        return None
    if any(getattr(span, "status", "") == "proven"
           and getattr(span, "cls", "") in ("pii", "secret")
           for span in decision.spans):
        return None
    combo = set(reid_info.get("combination") or []) if reid_info is not None else {str(item.get("kind", "")) for item in (current or [])}
    if combo & set(reid_module.CONTACT_KINDS):
        return None
    if not reid_module.has_encyclopedic_framing(text, conversation):
        return None
    reasons = list(decision.reasons) + [
        "verified public figure in encyclopedic framing -> ALLOW",
    ]
    evidence = dict(decision.evidence or {})
    sent_text = decision.sent_text if decision.decision == "WARN" else text
    downgraded = Decision(
        decision="ALLOW",
        decision_id=uuid.uuid4().hex,
        spans=decision.spans,
        sent_text=sent_text,
        placeholders=dict(decision.placeholders or {}),
        resolved_tier=1,
        latency_ms=dict(decision.latency_ms),
        reasons=reasons,
        evidence=evidence,
    )
    downgraded.doc_type = decision.doc_type
    downgraded.doc_warning = decision.doc_warning
    if trace_out is not None:
        trace_out.append(
            {"step": "intent.public_figure", "tool": "glimmer",
             "detail": "verified public figure, encyclopedic framing",
             "result": "OK", "ms": 0.0}
        )
    return downgraded


def _apply_person_search(
    text: str,
    decision: Decision,
    conversation: list[str] | None,
    current: list[dict[str, Any]] | None,
    person_search_judge_fn: Any,
    trace_out: list[dict[str, Any]] | None,
    mode: str = "tier1",
) -> Decision:
    """Layer person-search intent over a finished re-id decision (T33).

    Runs the local-Glimmer intent check and upgrades to WARN when the
    verified claim meets the bar. Never blocks and never downgrades a
    BLOCK; HOLD keeps its decision with the person-search payload
    attached, except a cue-only HOLD in send mode which becomes WARN so
    the user can review it. REDACT outranks WARN and keeps its
    redaction. Evidence carries labels and fingerprints only.
    """
    try:
        from . import person_search as person_search_module
    except Exception:
        return decision
    try:
        result = person_search_module.check_person_search(
            text,
            conversation=conversation,
            current_findings=current,
            judge_fn=person_search_judge_fn,
            trace_out=trace_out,
        )
    except Exception:
        return decision
    if not result.warn:
        downgraded = _downgrade_verified_public_figure(
            text, decision, conversation, current, result, trace_out,
        )
        if downgraded is not None:
            from .judge import contains_classifier_injection

            if contains_classifier_injection(text):
                decision.reasons.append("text aimed at the safety check")
                return decision
            return downgraded
        return decision
    evidence: dict[str, Any] = dict(decision.evidence or {})
    evidence["person_search"] = {
        "banner": result.banner,
        "inferred": list(result.inferred_labels),
        "confidence": result.confidence,
        "subject_fingerprint": result.subject_fingerprint,
        "inferred_fingerprints": list(result.inferred_fingerprints),
        "generalised_text": result.generalised_text,
    }
    score_line = "intent.person_search: private-person search -> WARN"
    if decision.decision == "BLOCK":
        decision.reasons.append(score_line)
        decision.evidence.update(evidence)
        return decision
    if decision.decision == "HOLD":
        if mode == "send" and _is_cue_only_hold(decision):
            reasons = list(decision.reasons) + [
                score_line,
                "person-search intent needs user review -> WARN",
            ]
            warn_decision = _warn_from_hold(text, decision, evidence, reasons)
            return warn_decision
        decision.reasons.append(score_line)
        decision.evidence.update(evidence)
        return decision
    reasons = list(decision.reasons) + [
        score_line,
        "person-search intent needs user review -> WARN",
    ]
    if decision.decision == "REDACT":
        decision.reasons.extend(reasons[len(decision.reasons):])
        decision.evidence.update(evidence)
        return decision
    if decision.decision == "WARN":
        return Decision(
            decision="WARN",
            decision_id=decision.decision_id,
            spans=decision.spans,
            sent_text=decision.sent_text,
            placeholders=dict(decision.placeholders),
            resolved_tier=1,
            latency_ms=dict(decision.latency_ms),
            reasons=reasons,
            evidence=evidence,
        )
    return Decision(
        decision="WARN",
        decision_id=uuid.uuid4().hex,
        spans=decision.spans,
        sent_text=text,
        placeholders={},
        resolved_tier=1,
        latency_ms=dict(decision.latency_ms),
        reasons=reasons,
        evidence=evidence,
    )


def gate_reid(
    text: str,
    policy: Policy,
    registry: CanaryRegistry | None = None,
    conversation: list[str] | None = None,
    residual: bool = False,
    judge_fn: Any = None,
    mode: str = "tier1",
    trace_out: list[dict[str, Any]] | None = None,
    person_search_judge_fn: Any = None,
) -> Decision:
    """Gate with re-identification scoring layered on top (alias of gate).

    Kept for backwards compatibility: :func:`gate` now runs the full
    check itself, so this forwards to it with the same arguments.
    ``residual`` runs the residual-context check with ``judge_fn``
    (default: local Muse Glimmer, never a cloud model); judge errors
    hold fail-closed, except person-search which falls back to the
    code-only result.
    """
    return gate(
        text, policy, registry,
        mode=mode, trace_out=trace_out,
        conversation=conversation, residual=residual, judge_fn=judge_fn,
        person_search_judge_fn=person_search_judge_fn,
    )


def gate(
    text: str,
    policy: Policy,
    registry: CanaryRegistry | None = None,
    choices: list[dict[str, Any]] | None = None,
    mode: str = "tier1",
    trace_out: list[dict[str, Any]] | None = None,
    conversation: list[str] | None = None,
    residual: bool = False,
    judge_fn: Any = None,
    person_search_judge_fn: Any = None,
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
    # Proven-secret BLOCK short-circuit (T63c): a secret, canary or
    # protected key is already certain from Tier 1 code detectors, and
    # BLOCK outranks every other outcome. Return at once without any
    # model call (Glimmer Tier 2, person search, cue review,
    # contract/model checks). Placeholders stay empty and the log
    # record is unchanged apart from the added skip reason.
    skip_models = (
        decision.decision == "BLOCK"
        and has_proven_secret_block(decision.spans)
    )
    # Full-bundle BLOCK short-circuit (T66c): Tier 1 code alone
    # proves name + dob + (street or postcode) + (phone or email),
    # so confirm with the code-only assessor (stub judge: no
    # Glimmer) and return BLOCK at once without any model call
    # (live suspicions, person search, residual or cue review).
    if not skip_models and tier1_proves_identity_bundle(
        text, conversation
    ):
        try:
            _probe = tier1.scan_reid(text)
            _candidate = reid_module.assess(
                text, conversation=conversation,
                current_findings=_probe,
                public_figure_judge_fn=lambda _t: None,
            )
            if _candidate.outcome == "BLOCK":
                tier1.scan_reid(text, trace_out=trace_out)
                reid_module.trace_combine(
                    trace_out, _candidate.score,
                    _candidate.combination, _candidate.outcome,
                )
                _evidence = dict(decision.evidence or {})
                _evidence["reid"] = {
                    "score": _candidate.score,
                    "combination": list(_candidate.combination),
                    "outcome": _candidate.outcome,
                    "banner": _candidate.banner,
                    "generalised_text": _candidate.generalised_text,
                }
                _block_line = "reid: score={} kinds={} -> BLOCK".format(
                    _candidate.score, "+".join(_candidate.combination))
                decision = Decision(
                    decision="BLOCK",
                    decision_id=uuid.uuid4().hex,
                    spans=decision.spans,
                    sent_text=None,
                    placeholders={},
                    resolved_tier=1,
                    latency_ms=dict(decision.latency_ms),
                    reasons=[*decision.reasons, _block_line,
                             "full identity bundle -> BLOCK"
                             " (model checks skipped)"],
                    evidence=_evidence,
                )
                _attach_doc_classification(decision, text, trace_out)
                try:
                    from . import rule_catalog as _catalog

                    decision.rule_ids = _catalog.rule_ids_for_decision(
                        decision)
                except Exception:
                    decision.rule_ids = []
                _append_gate_trace(
                    trace_out, decision, choices,
                    (time.perf_counter() - started) * 1000,
                )
                return decision
        except Exception:
            pass
    if skip_models:
        decision.reasons.append(
            "proven secret -> BLOCK (model checks skipped)"
        )
    _attach_doc_classification(decision, text, trace_out)
    _doc_type, _doc_warning = decision.doc_type, decision.doc_warning
    if skip_models:
        try:
            from . import rule_catalog as _catalog

            decision.rule_ids = _catalog.rule_ids_for_decision(decision)
        except Exception:
            decision.rule_ids = []
        _append_gate_trace(
            trace_out, decision, choices,
            (time.perf_counter() - started) * 1000,
        )
        return decision
    try:
        decision = _apply_reid(
            text, decision, conversation, residual, judge_fn, mode,
            trace_out, person_search_judge_fn=person_search_judge_fn,
        )
    except Exception as exc:
        if decision.decision != "BLOCK":
            decision = Decision(
                decision="HOLD",
                decision_id=uuid.uuid4().hex,
                spans=decision.spans,
                sent_text=None,
                placeholders={},
                resolved_tier=1,
                latency_ms=dict(decision.latency_ms),
                reasons=[*decision.reasons,
                         f"reid layer error ({type(exc).__name__}) -> HOLD"],
                evidence=dict(decision.evidence or {}),
                doc_type=_doc_type,
                doc_warning=_doc_warning,
            )
    if decision.doc_type is None:
        decision.doc_type = _doc_type
    if decision.doc_warning is None:
        decision.doc_warning = _doc_warning
        if _doc_warning and isinstance(decision.evidence, dict):
            decision.evidence.setdefault("doc_warning",
                                         _doc_warning)
            if _doc_type:
                decision.evidence.setdefault("doc_type", _doc_type)
    try:
        from . import rule_catalog as _catalog
        decision.rule_ids = _catalog.rule_ids_for_decision(decision)
    except Exception:
        decision.rule_ids = []
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
