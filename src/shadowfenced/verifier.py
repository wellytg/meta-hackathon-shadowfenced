"""Tier 3 verifier: the model proposes, code disposes.

``verify`` checks a judge's claim against deterministic code evidence and
returns a ``Verdict``. The model span must be an exact substring of the
gated text — offsets are computed by code, never trusted from the model.
A claim is confirmed only when code corroborates it:

- secret: the span is (or decodes via identity/base64/hex/rot13/reversed/
  url to) a registry canary, matches a provider shape or password
  assignment, or carries a high-entropy token (Shannon > 4.0);
- pii/business: a Tier 1 PII pattern or policy word matches inside the span;
- beacon: the sentence holding the span has both a location token and a
  secret-ish value nearby (a Tier 1 secret span or entropy token).

Anything else is unconfirmed, and the gate holds for a human.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import tier1
from .canary import CanaryRegistry, decode_encoding
from .policy import Policy
from .spans import Span, span_from_cp_range

_DECODINGS = ("base64", "hex", "rot13", "reversed", "url")


@dataclass
class Verdict:
    """One verification outcome."""

    confirmed: bool
    cls: str
    span: Span | None  # code-found offsets when confirmed, else None
    why: str


def _judgement_field(judgement: Any, name: str, default: Any = None) -> Any:
    if isinstance(judgement, dict):
        return judgement.get(name, default)
    return getattr(judgement, name, default)


def _confirmed_span(
    text: str,
    start: int,
    end: int,
    cls: str,
    subclass: str,
    policy: Policy,
    confidence: float,
    message: str,
    why: str,
) -> Span:
    """Build a Tier 3 code-proven span at code-found offsets."""
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.9
    return span_from_cp_range(
        text,
        start,
        end,
        cls=cls,
        subclass=subclass,
        tier=3,
        proof="code",
        status="proven",
        message=message,
        why=why,
        suggested_action=policy.action_for(cls),  # type: ignore[arg-type]
        can_keep=policy.keepable(cls),  # type: ignore[arg-type]
        confidence=confidence_value,
    )


def _resolve_canary(
    span_text: str, registry: CanaryRegistry | None
) -> tuple[str, str] | None:
    """Return (canary, encoding) when the span resolves to a canary."""
    stripped = span_text.strip().strip("\"'")
    if registry is not None and stripped and stripped in registry:
        return stripped, "identity"
    if stripped:
        for method in _DECODINGS:
            try:
                decoded = decode_encoding(stripped, method)
            except Exception:
                continue
            if decoded and registry is not None and decoded in registry:
                return decoded, method
        if registry is not None:
            for value in registry.values():
                if value and value in stripped:
                    return value, "identity"
    return None


def _verify_secret(
    text: str,
    start: int,
    span_text: str,
    judgement: Any,
    policy: Policy,
    registry: CanaryRegistry | None,
) -> Verdict:
    confidence = _judgement_field(judgement, "confidence", 0.9)
    resolved = _resolve_canary(span_text, registry)
    if resolved is not None:
        _, encoding = resolved
        return Verdict(
            True,
            "secret",
            _confirmed_span(
                text, start, start + len(span_text), "secret", "canary",
                policy, confidence,
                "Model span resolves to a planted canary.",
                f"span decodes ({encoding}) to a registry canary",
            ),
            f"span decodes ({encoding}) to a registry canary",
        )
    inner = tier1.scan(span_text, policy, registry)
    proven = [
        s for s in inner if s.cls == "secret" and s.status == "proven"
    ]
    if proven:
        winner = next(
            (s for s in proven if s.subclass == "canary"), proven[0]
        )
        if winner.subclass == "canary":
            return Verdict(
                True,
                "secret",
                _confirmed_span(
                    text, start, start + len(span_text), "secret",
                    "canary", policy, confidence,
                    "Model span resolves to a planted canary.",
                    "span contains a registry canary value",
                ),
                "span contains a registry canary value",
            )
        return Verdict(
            True,
            "secret",
            _confirmed_span(
                text, start, start + len(span_text), "secret",
                winner.subclass, policy, confidence,
                "Code corroborates the model's secret claim.",
                f"provider or password shape inside the span "
                f"({winner.subclass})",
            ),
            f"provider or password shape inside the span ({winner.subclass})",
        )
    if any(s.cls == "secret" for s in inner):
        return Verdict(
            True,
            "secret",
            _confirmed_span(
                text, start, start + len(span_text), "secret",
                "entropy_token", policy, confidence,
                "Code corroborates the model's secret claim.",
                "high-entropy token (Shannon > 4.0) inside the span",
            ),
            "high-entropy token (Shannon > 4.0) inside the span",
        )
    return Verdict(
        False,
        "secret",
        None,
        "no canary, provider shape, password assignment, or "
        "high-entropy token in the span",
    )


def _verify_pii_business(
    text: str,
    start: int,
    end: int,
    span_text: str,
    judgement: Any,
    tier1_spans: list[Span],
    policy: Policy,
    registry: CanaryRegistry | None,
) -> Verdict:
    cls = _judgement_field(judgement, "cls", "pii")
    confidence = _judgement_field(judgement, "confidence", 0.9)
    inner = tier1.scan(span_text, policy, registry)
    hits = [
        s for s in inner
        if s.cls in ("pii", "business") and s.status == "proven"
    ]
    if not hits:
        for span in tier1_spans:
            if (
                span.cls in ("pii", "business")
                and span.status == "proven"
                and span.cp_start < end
                and start < span.cp_end
            ):
                hits.append(span)
                break
    if hits:
        match = hits[0]
        return Verdict(
            True,
            cls,
            _confirmed_span(
                text, start, end, cls, match.subclass, policy, confidence,
                "Code corroborates the model's claim.",
                f"tier1 {match.subclass} match inside the span",
            ),
            f"tier1 {match.subclass} match inside the span",
        )
    return Verdict(
        False, cls, None,
        "no tier1 pii pattern or policy word inside the span",
    )


def _verify_beacon(
    text: str,
    start: int,
    end: int,
    span_text: str,
    judgement: Any,
    tier1_spans: list[Span],
    policy: Policy,
    registry: CanaryRegistry | None,
) -> Verdict:
    confidence = _judgement_field(judgement, "confidence", 0.9)
    sentence = None
    for sent_start, sent_end in tier1._sentences(text):
        if sent_start <= start < sent_end:
            sentence = (sent_start, sent_end)
            break
    if sentence is None:
        return Verdict(False, "beacon", None, "span sits in no sentence")
    sent_start, sent_end = sentence
    body = text[sent_start:sent_end]
    location = any(rx.search(body) for rx in tier1._LOCATION_RES)
    if not location:
        return Verdict(
            False, "beacon", None, "sentence has no location token",
        )
    nearby = any(
        span.cls == "secret" and span.cp_start < sent_end
        and sent_start < span.cp_end
        for span in tier1_spans
    )
    if not nearby:
        nearby = any(
            s.cls == "secret"
            for s in tier1.scan(body, policy, registry)
        )
    if not nearby:
        return Verdict(
            False, "beacon", None,
            "location token found but no secret-ish value nearby",
        )
    return Verdict(
        True,
        "beacon",
        _confirmed_span(
            text, start, end, "beacon", "beacon_cue", policy, confidence,
            "Code corroborates the model's beacon claim.",
            "location token plus secret-ish value in one sentence",
        ),
        "location token plus secret-ish value in one sentence",
    )


def verify(
    text: str,
    judgement: Any,
    tier1_spans: list[Span],
    policy: Policy,
    registry: CanaryRegistry | None = None,
) -> Verdict:
    """Verify one model judgement against code evidence."""
    cls = _judgement_field(judgement, "cls", "benign")
    span_text = _judgement_field(judgement, "span", "") or ""
    if not isinstance(span_text, str) or not span_text:
        return Verdict(False, str(cls), None, "model span is empty")
    if span_text not in text:
        return Verdict(
            False, str(cls), None,
            "model span is not an exact substring of the text",
        )
    if cls == "benign":
        return Verdict(
            False, "benign", None, "model judged the span benign",
        )
    start = text.find(span_text)
    end = start + len(span_text)
    if cls == "secret":
        return _verify_secret(text, start, span_text, judgement, policy, registry)
    if cls in ("pii", "business"):
        return _verify_pii_business(
            text, start, end, span_text, judgement, tier1_spans, policy,
            registry,
        )
    if cls == "beacon":
        return _verify_beacon(
            text, start, end, span_text, judgement, tier1_spans, policy,
            registry,
        )
    return Verdict(False, str(cls), None, f"unknown class {cls!r}")
