"""Person-search intent (T33): Glimmer reads the whole statement.

Code finds quasi-identifiers and scores combinations (see reid module),
but cannot tell intent: "When was [a historical figure] born?" is
ordinary research while "Find everything you can about Dana Whitlock,
born 14 March 1991, Copperleaf Drive, Fairhaven" is a search for a
private person.

This module asks local Muse Glimmer -- never a cloud model -- with a
typed schema when the message (or conversation) looks like a
people search, then verifies the claim in code (Tier 3):

- trigger: a person's name plus at least one other quasi-identifier,
  or people-search wording ("find", "look up", "everything about",
  "where does ... live", "who is", "dig up", "background on",
  "contact details for");
- Glimmer answers ``{person_search, subject_public, subject, inferred,
  confidence}`` where ``subject`` is an exact name substring (or "")
  and each ``inferred`` item is tied to an exact substring;
- code keeps only ``subject``/``inferred`` items that appear exactly
  in the message. ``person_search`` true + ``subject_public`` false +
  confidence >= 0.6 with a verified subject => WARN (never block).
  Public figures in public roles and historical figures are not flagged.

Raw values never leave this module except inside caller-held return
objects: trace helpers emit labels and short sha256 fingerprints only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, field_validator

from . import reid as reid_module

# --- Trigger: people-search wording ------------------------------------------
_SEARCH_PATTERNS = (
    r"\bfind\b",
    r"\blook\s*up\b",
    r"\beverything\s+about\b",
    r"\bwhere\s+does\b.*\blive\b",
    r"\bwhere\s+do\b.*\blive\b",
    r"\bwho\s+is\b",
    r"\bdig\s+up\b",
    r"\bbackground\s+on\b",
    r"\bcontact\s+details?\s+for\b",
)
_SEARCH_RE = re.compile("|".join(_SEARCH_PATTERNS), re.IGNORECASE | re.DOTALL)


def has_search_wording(text: str) -> bool:
    """True when people-search wording appears in the text."""
    if not text:
        return False
    return _SEARCH_RE.search(text) is not None


def should_check(text: str, conversation: list[str] | None = None) -> bool:
    """True when Glimmer should be asked about person-search intent.

    Trigger: the message (or conversation) holds a person's name plus
    at least one other quasi-identifier, or people-search wording.
    """
    combined = "\n".join([*(conversation or []), text]) if text else "\n".join(conversation or [])
    if not combined:
        return False
    if has_search_wording(combined):
        return True
    try:
        findings = reid_module.extract_quasi_identifiers(combined)
    except Exception:
        return False
    kinds = {item.get("kind") for item in findings}
    return "name" in kinds and len(kinds) >= 2


# --- Typed Glimmer answer ------------------------------------------------------
class PersonSearchJudgement(BaseModel):
    """One Glimmer verdict on person-search intent (no free-text reason)."""

    person_search: bool
    subject_public: bool
    subject: str
    inferred: list[str]
    confidence: float

    @field_validator("confidence")
    @classmethod
    def _unit_range(cls, value: float) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("confidence must be a number")
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError("confidence must be within 0..1")
        return float(value)

    @field_validator("subject")
    @classmethod
    def _subject_is_str(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("subject must be a string")
        return value

    @field_validator("inferred")
    @classmethod
    def _inferred_is_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("inferred must be a list")
        return [item for item in value if isinstance(item, str)]


PERSON_SEARCH_SYSTEM = (
    "Decide whether a user message is a search for a private person. "
    "person_search is true when the message tries to find, identify, or "
    "collect details about a named private individual (including asking "
    "where they live, their background, or their contact details). "
    "Ordinary research about a historical figure or a public figure in "
    "their public role is not a person search (person_search false, "
    "subject_public true when the subject is public/historical). "
    "subject is the exact full name copied word-for-word from the text, "
    "or empty when there is none. inferred lists short facts the message "
    "reveals or seeks, each copied word-for-word from the text. "
    "confidence is 0-1. JSON only."
)


def local_glimmer_person_search(
    text: str,
    timeout_s: float = 10.0,
    transport: Any = None,
) -> dict[str, Any] | None:
    """Ask local Muse Glimmer about person-search intent; None on failure.

    Local Ollama only (``/api/chat`` on the configured Ollama URL with the
    configured judge model). Never touches a cloud model or the Meta
    upstream client. Any transport error, timeout or unparsable answer
    returns None so the caller falls back to the code-only result.
    """
    try:
        import httpx

        from .config import get_settings

        settings = get_settings()
        # Honour the Tier 2 off switch: tests and eval pin it to skip
        # every model call deterministically.
        try:
            from .judge import tier2_mode

            if tier2_mode() == "off":
                return None
        except Exception:
            pass
        url = settings.ollama_url.rstrip("/") + "/api/chat"
        schema = PersonSearchJudgement.model_json_schema()
        body = {
            "model": settings.judge_model,
            "messages": [
                {"role": "system", "content": PERSON_SEARCH_SYSTEM},
                {"role": "user", "content": text[:2000]},
            ],
            "format": schema,
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "seed": 7, "num_ctx": 2048, "num_predict": 150},
        }
        timeout = float(timeout_s) if timeout_s else 10.0
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.post(url, json=body)
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        message = payload.get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            return None
        cleaned = content.strip()
        import json as _json

        try:
            return _json.loads(cleaned)
        except ValueError:
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start == -1 or end <= start:
                return None
            try:
                return _json.loads(cleaned[start : end + 1])
            except ValueError:
                return None
    except Exception:
        return None


def _parse_judgement(raw: Any) -> PersonSearchJudgement | None:
    """Validate a judge answer; None when unusable (never raises)."""
    if raw is None:
        return None
    try:
        if hasattr(raw, "model_dump"):
            return PersonSearchJudgement.model_validate(raw.model_dump())
        if isinstance(raw, dict):
            return PersonSearchJudgement.model_validate(raw)
    except Exception:
        return None
    return None


# --- Banner labels (plain words, never values) ----------------------------------
_THEIR_LABEL: dict[str, str] = {
    "name": "their full name",
    "dob": "their date of birth",
    "age": "their age",
    "street": "their street",
    "postcode": "their postcode",
    "city": "their town",
    "employer": "their employer",
    "jobtitle": "their job title",
    "school": "their school",
    "gender": "their gender",
    "medical": "their health details",
    "plate": "their vehicle",
    "vehicle": "their vehicle",
    "phone": "their phone number",
    "email": "their email address",
    "dated_event": "dates",
}

_STREET_HINTS = (
    "street", "address", "lane", "drive", "road", "avenue", "court",
    "cottage", "row", "place", "terrace", "boulevard", "way", "pike",
)


def _label_for_fact(fact: str, findings: list[dict[str, Any]]) -> str:
    """Map one verified inferred fact to plain-words ``their X`` label."""
    for item in findings or []:
        value = str(item.get("value", "") or "")
        if value and (value in fact or fact in value):
            kind = str(item.get("kind", "") or "")
            if kind in _THEIR_LABEL:
                return _THEIR_LABEL[kind]
    low = fact.lower()
    if "name" in low:
        return "their full name"
    if "birth" in low or "born" in low or "dob" in low or "date of birth" in low:
        return "their date of birth"
    if "phone" in low:
        return "their phone number"
    if "email" in low:
        return "their email address"
    if "contact" in low:
        return "their contact details"
    if any(hint in low for hint in _STREET_HINTS):
        return "their street"
    if "postcode" in low or "zip" in low:
        return "their postcode"
    if "town" in low or "city" in low or "village" in low or "live" in low or "reside" in low:
        return "their town"
    if "employ" in low or "workplace" in low or "company" in low or "clinic" in low:
        return "their employer"
    if "job" in low or "occupation" in low or "works as" in low:
        return "their job title"
    if "school" in low or "college" in low or "university" in low:
        return "their school"
    if "health" in low or "medical" in low or "allergy" in low or "pregnan" in low:
        return "their health details"
    if "age" in low or "years old" in low:
        return "their age"
    return "personal details"


def banner_for_labels(labels: list[str]) -> str:
    """Plain-words person-search banner listing labels only, never values."""
    seen: list[str] = []
    for label in labels:
        if label and label not in seen:
            seen.append(label)
    if not seen:
        seen = ["personal details"]
    if len(seen) == 1:
        listing = seen[0]
    elif len(seen) == 2:
        listing = f"{seen[0]} and {seen[1]}"
    else:
        listing = ", ".join(seen[:-1]) + f" and {seen[-1]}"
    return (
        "This looks like a search for a private person. "
        f"From this message alone, an AI can tell: {listing}. "
        "Sending it shares those details with the AI provider, "
        "which may refuse or log the request."
    )


@dataclass
class PersonSearchResult:
    """Outcome of the person-search intent check."""

    warn: bool = False
    subject_public: bool | None = None
    verified_subject: bool = False
    triggered: bool = False
    banner: str = ""
    inferred_labels: list[str] = field(default_factory=list)
    confidence: float = 0.0
    subject_fingerprint: str = ""
    inferred_fingerprints: list[str] = field(default_factory=list)
    generalised_text: str = ""
    reason: str = ""


def check_person_search(
    text: str,
    conversation: list[str] | None = None,
    current_findings: list[dict[str, Any]] | None = None,
    judge_fn: Callable[[str], Any] | None = None,
    trace_out: list[dict[str, Any]] | None = None,
    transport: Any = None,
) -> PersonSearchResult:
    """Run the person-search intent check (Tier 3: model proposes, code disposes).

    Returns a :class:`PersonSearchResult`; ``warn`` is True only when the
    verified Glimmer claim meets the T33 bar (person_search true,
    subject_public false, confidence >= 0.6 with a verified subject).
    Every failure -- no trigger, unavailable Glimmer, unparsable or
    unverifiable claim -- returns ``warn=False`` so the caller falls back
    to the code-only re-identification result. The trace step
    ``intent.person_search`` carries labels and fingerprints only.
    """
    prior = list(conversation or [])
    combined = "\n".join([*prior, text]) if text else "\n".join(prior)
    if not combined:
        _trace(trace_out, "no text", "OK")
        return PersonSearchResult(reason="no text")
    if not should_check(text, prior):
        _trace(trace_out, "no trigger", "OK")
        return PersonSearchResult(reason="no trigger")
    judge = judge_fn if judge_fn is not None else local_glimmer_person_search
    try:
        if judge_fn is not None:
            raw = judge(combined)
        else:
            raw = judge(combined, transport=transport) if transport is not None else judge(combined)
    except Exception:
        raw = None
    parsed = _parse_judgement(raw)
    if parsed is None:
        _trace(trace_out, "Glimmer could not check intent", "OK")
        return PersonSearchResult(triggered=True, reason="Glimmer could not check intent")
    # Tier 3 verification: keep only claims tied to exact substrings.
    verified_subject = parsed.subject if parsed.subject and parsed.subject in combined else ""
    verified_inferred = [
        clue for clue in (parsed.inferred or [])
        if isinstance(clue, str) and clue and clue in combined
    ]
    try:
        confidence = float(parsed.confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    findings = list(current_findings) if current_findings is not None else []
    if not findings:
        try:
            findings = reid_module.extract_quasi_identifiers(combined)
        except Exception:
            findings = []
    if (
        parsed.person_search
        and not parsed.subject_public
        and confidence >= 0.6
        and verified_subject
    ):
        labels = [_label_for_fact(fact, findings) for fact in verified_inferred]
        # The verified name itself is always inferable: name it first.
        if "their full name" not in labels:
            labels = ["their full name", *labels]
        banner = banner_for_labels(labels)
        try:
            generalised, _ = reid_module.generalise(
                text, reid_module.extract_quasi_identifiers(text)
            )
        except Exception:
            generalised = ""
        result = PersonSearchResult(
            warn=True,
            triggered=True,
            banner=banner,
            inferred_labels=labels,
            confidence=confidence,
            subject_fingerprint=reid_module.fingerprint(verified_subject),
            inferred_fingerprints=sorted(
                {reid_module.fingerprint(clue) for clue in verified_inferred}
            ),
            generalised_text=generalised,
            reason="verified private-person search -> WARN",
            subject_public=False,
            verified_subject=True,
        )
        _trace_warn(trace_out, result, parsed)
        return result
    # Verified drop (unverifiable claims, public subject, low confidence,
    # or not a person search): no warning, never raw values in trace.
    _trace_drop(
        trace_out, parsed.person_search, parsed.subject_public,
        confidence, bool(verified_subject), len(verified_inferred),
    )
    return PersonSearchResult(
        triggered=True,
        confidence=confidence,
        subject_fingerprint=reid_module.fingerprint(verified_subject) if verified_subject else "",
        inferred_fingerprints=sorted(
            {reid_module.fingerprint(clue) for clue in verified_inferred}
        ),
        reason="no verified private-person search",
        subject_public=parsed.subject_public,
        verified_subject=bool(verified_subject),
    )


def _trace(
    trace: list[dict[str, Any]] | None, detail: str, result: str,
) -> None:
    if trace is None:
        return
    trace.append({
        "step": "intent.person_search", "tool": "glimmer",
        "detail": detail, "result": result, "ms": 0.0,
    })


def _trace_warn(
    trace: list[dict[str, Any]] | None,
    result: PersonSearchResult,
    parsed: PersonSearchJudgement,
) -> None:
    if trace is None:
        return
    marks = [f"{label}~{fp}" for label, fp in zip(
        result.inferred_labels, result.inferred_fingerprints + [""] * len(result.inferred_labels)
    )]
    # Pair labels with fingerprints deterministically without raw values.
    fps = sorted(result.inferred_fingerprints)
    detail = (
        f"person_search=true subject_public=false "
        f"confidence={result.confidence:.2f} "
        f"subject_fp={result.subject_fingerprint} "
        f"inferred={len(result.inferred_labels)}"
    )
    if fps:
        detail += " [" + ", ".join(fps) + "]"
    void = parsed  # judgement labels only; raw values never enter the trace.
    _ = void
    trace.append({
        "step": "intent.person_search", "tool": "glimmer",
        "detail": detail, "result": "WARN", "ms": 0.0,
    })
    _ = marks


def _trace_drop(
    trace: list[dict[str, Any]] | None,
    person_search: bool, subject_public: bool,
    confidence: float, has_subject: bool, n_inferred: int,
) -> None:
    if trace is None:
        return
    detail = (
        f"person_search={str(person_search).lower()} "
        f"subject_public={str(subject_public).lower()} "
        f"confidence={confidence:.2f} "
        f"verified_subject={str(bool(has_subject)).lower()} "
        f"verified_inferred={n_inferred}"
    )
    trace.append({
        "step": "intent.person_search", "tool": "glimmer",
        "detail": detail, "result": "OK", "ms": 0.0,
    })
