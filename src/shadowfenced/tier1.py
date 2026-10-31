"""Tier 1: deterministic code detectors (patterns, canaries, entropy, policy words).

``scan(text, policy, registry)`` returns a list of ``Span`` objects sorted by
offset. Proven spans (canaries, key shapes, PII, policy words) map to the
per-class policy action; ambiguous spans (entropy, beacon cues) suggest
``review``. Provider key prefixes are assembled at runtime from fragments so
no real-format literal ever appears in this file.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from .canary import PREFIX as CANARY_PREFIX
from .canary import CanaryRegistry, encodings
from .policy import Policy
from .spans import Span, merge_overlaps, span_from_cp_range

# Any canary token, even one minted from an unknown seed.
_ANY_CANARY_RE = re.compile(re.escape(CANARY_PREFIX) + r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*")

_PASSWORD_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token)\s*[:=]\s*\S+"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN_RE = re.compile(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b")
_EMP_RE = re.compile(r"\bEMP-\d{6}\b")
_NAME_RE = re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")
_CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ \t-]?){13,19}(?!\d)")
_US_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s().-]?)?(?:\(\d{3}\)|\d{3})[\s().-]?\d{3}[\s().-]?\d{4}(?!\d)"
)
_INTL_PHONE_RE = re.compile(r"\+\d[\d\s().-]{5,}\d")

_SECRET_NOUN_RE = re.compile(
    r"(?i)\b(keys?|creds?|credentials?|passwords?|tokens?|secrets?)\b"
)
_LOCATION_RES = (
    re.compile(r"~/[^\s]*"),  # ~/ path
    re.compile(r"(?:/[A-Za-z0-9_.-]+){2,}"),  # absolute file path
    re.compile(r"(?i)\bvault:[^\s]+"),  # vault path
    re.compile(r"\b(?:[A-Za-z0-9-]+\.)+(?:com|io|net|org|internal|local|dev)\b"),  # host
    re.compile(r"\b[A-Za-z0-9_-]+\.(?:py|js|ts|json|yaml|yml|env|ini|cfg|csv|txt|md|log|pem|key)\b"),  # filename
    re.compile(r"(?i)\b\w*folder\w*\b"),  # folder word
    re.compile(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+\b"),  # relative path
)
_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\b")
# Sentence boundary: a newline run, or . ! ? followed by whitespace/end.
# A bare "." inside a filename or hostname is not a boundary.
_SENT_BOUNDARY_RE = re.compile(r"\n+|(?<=[.!?])(?=\s|$)")


def _sentences(text: str) -> list[tuple[int, int]]:
    """Split text into (cp_start, cp_end) sentence ranges."""
    bounds = [0]
    for match in _SENT_BOUNDARY_RE.finditer(text):
        bounds.append(match.end())
    bounds.append(len(text))
    return [
        (start, end)
        for start, end in zip(bounds, bounds[1:])
        if text[start:end].strip()
    ]


def _provider_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Provider key shapes; prefixes are joined here at runtime."""
    cloud = "AK" + "IA" + r"[0-9A-Z]{16}(?![0-9A-Z])"
    dev = "gh" + "p_" + r"[A-Za-z0-9]{36}(?![A-Za-z0-9])"
    live = "s" + "k-" + r"[A-Za-z0-9]{20,}(?![A-Za-z0-9])"
    bot = "xox" + "b-" + r"[A-Za-z0-9-]{10,}"
    return [
        ("cloud_key", re.compile(r"(?<![0-9A-Z])" + cloud)),
        ("dev_token", re.compile(r"(?<![A-Za-z0-9])" + dev)),
        ("live_secret", re.compile(r"(?<![A-Za-z0-9])" + live)),
        ("bot_token", re.compile(bot)),
    ]


def shannon_entropy(token: str) -> float:
    """Shannon entropy of a token in bits per character."""
    if not token:
        return 0.0
    counts = Counter(token)
    length = len(token)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def _git_sha_shape(token: str) -> bool:
    return re.fullmatch(r"[0-9a-fA-F]{7,64}", token) is not None


def _uuid_shape(token: str) -> bool:
    return (
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            token,
        )
        is not None
    )


def _allowlisted_token(policy: Policy, token: str) -> bool:
    shapes = set(policy.allowlist.shapes)
    if "git-sha" in shapes and _git_sha_shape(token):
        return True
    if "uuid" in shapes and _uuid_shape(token):
        return True
    return False


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        digit = ord(ch) - ord("0")
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _emit(
    text: str,
    cp_start: int,
    cp_end: int,
    cls: str,
    subclass: str,
    status: str,
    message: str,
    why: str,
    policy: Policy,
    confidence: float,
) -> Span:
    action = "review" if status == "ambiguous" else policy.action_for(cls)  # type: ignore[arg-type]
    return span_from_cp_range(
        text,
        cp_start,
        cp_end,
        cls=cls,
        subclass=subclass,
        status=status,
        message=message,
        why=why,
        suggested_action=action,
        can_keep=policy.keepable(cls),  # type: ignore[arg-type]
        confidence=confidence,
    )


def _literal_spans(
    text: str, needle: str, cls: str, subclass: str, status: str,
    message: str, why: str, policy: Policy, confidence: float,
) -> list[Span]:
    spans: list[Span] = []
    start = 0
    while True:
        found = text.find(needle, start)
        if found < 0:
            return spans
        spans.append(
            _emit(text, found, found + len(needle), cls, subclass, status,
                  message, why, policy, confidence)
        )
        start = found + max(len(needle), 1)


def _scan_canaries(
    text: str, registry: CanaryRegistry | None, policy: Policy,
) -> list[Span]:
    spans: list[Span] = []
    for match in _ANY_CANARY_RE.finditer(text):
        spans.append(
            _emit(
                text, match.start(), match.end(), "secret", "canary", "proven",
                "Looks like a planted canary token.",
                f"matches {CANARY_PREFIX} token shape",
                policy, 0.99,
            )
        )
    if registry is None:
        return spans
    for value in registry.values():
        for form in encodings(value).values():
            if not form:
                continue
            spans.extend(
                _literal_spans(
                    text, form, "secret", "canary", "proven",
                    "Looks like a planted canary secret.",
                    "exact or encoded canary match", policy, 0.99,
                )
            )
        # Split across whitespace/newlines: arbitrary gaps between chars.
        split = r"\s*".join(re.escape(ch) for ch in value)
        for match in re.finditer(split, text):
            spans.append(
                _emit(
                    text, match.start(), match.end(), "secret", "canary",
                    "proven", "Looks like a planted canary split across text.",
                    "canary chars with whitespace/newlines between them",
                    policy, 0.95,
                )
            )
    return spans


def _scan_provider_keys(text: str, policy: Policy) -> list[Span]:
    spans: list[Span] = []
    for name, pattern in _provider_patterns():
        for match in pattern.finditer(text):
            spans.append(
                _emit(
                    text, match.start(), match.end(), "secret", "provider_key",
                    "proven", "Looks like a provider API key.",
                    f"matches {name} shape", policy, 0.95,
                )
            )
    for match in _PASSWORD_RE.finditer(text):
        spans.append(
            _emit(
                text, match.start(), match.end(), "secret",
                "password_assignment", "proven",
                "Looks like a password or secret assigned inline.",
                "password-like label followed by : or = and a value",
                policy, 0.9,
            )
        )
    return spans


def _scan_entropy(text: str, policy: Policy) -> list[Span]:
    spans: list[Span] = []
    for match in re.finditer(r"\S{20,}", text):
        raw = match.group()
        token = raw.strip(".,;:!?\"'()[]{}<>=")
        start = match.start() + (match.group().find(token) if token else 0)
        if len(token) < 20 or "@" in token:
            continue
        if _allowlisted_token(policy, token):
            continue
        if shannon_entropy(token) > 4.0:
            spans.append(
                _emit(
                    text, start, start + len(token), "secret", "entropy",
                    "ambiguous", "High-entropy token; might be a secret.",
                    f"length {len(token)}, Shannon entropy "
                    f"{shannon_entropy(token):.2f} bits/char",
                    policy, 0.55,
                )
            )
    return spans


def _scan_pii(text: str, policy: Policy) -> list[Span]:
    spans: list[Span] = []
    for match in _EMAIL_RE.finditer(text):
        domain = match.group().rsplit("@", 1)[1]
        if policy.email_allowlisted(domain):
            continue
        spans.append(
            _emit(
                text, match.start(), match.end(), "pii", "email", "proven",
                "Looks like an email address.",
                "email shape with a non-allowlisted domain", policy, 0.95,
            )
        )
    for match in _SSN_RE.finditer(text):
        spans.append(
            _emit(
                text, match.start(), match.end(), "pii", "ssn", "proven",
                "Looks like an SSN-shaped number.",
                "NNN-NN-NNNN shape", policy, 0.9,
            )
        )
    for match in _CARD_CANDIDATE_RE.finditer(text):
        digits = re.sub(r"[ \t-]", "", match.group())
        if 13 <= len(digits) <= 19 and digits.isdigit() and _luhn_valid(digits):
            spans.append(
                _emit(
                    text, match.start(), match.end(), "pii", "card", "proven",
                    "Looks like a payment card number.",
                    "13-19 digits passing the Luhn check", policy, 0.9,
                )
            )
    for match in _US_PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 7 <= len(digits) <= 15:
            spans.append(
                _emit(
                    text, match.start(), match.end(), "pii", "phone", "proven",
                    "Looks like a phone number.",
                    "phone digit grouping", policy, 0.85,
                )
            )
    for match in _INTL_PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 7 <= len(digits) <= 15:
            spans.append(
                _emit(
                    text, match.start(), match.end(), "pii", "phone", "proven",
                    "Looks like an international phone number.",
                    "leading + with 7-15 digits", policy, 0.85,
                )
            )
    for match in _EMP_RE.finditer(text):
        window_start = max(0, match.start() - 50)
        window_end = min(len(text), match.end() + 50)
        if _NAME_RE.search(text[window_start:window_end]):
            spans.append(
                _emit(
                    text, match.start(), match.end(), "pii", "employee_id",
                    "proven", "Looks like an employee ID next to a name.",
                    "EMP-NNNNNN near a capitalised name", policy, 0.9,
                )
            )
    return spans


def _codename_pattern(name: str) -> re.Pattern[str]:
    inner = r"[\s-]*".join(re.escape(ch) for ch in name)
    return re.compile(r"\b" + inner + r"\b", re.IGNORECASE)


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    inner = r"[\s-]+".join(re.escape(word) for word in phrase.split())
    return re.compile(r"\b" + inner + r"\b", re.IGNORECASE)


def _scan_business(text: str, policy: Policy) -> list[Span]:
    spans: list[Span] = []
    for name in policy.codename_names():
        for match in _codename_pattern(name).finditer(text):
            surface = match.group()
            all_caps = surface == surface.upper() and re.search(r"[A-Z]", surface)
            window_start = max(0, match.start() - 40)
            window_end = min(len(text), match.end() + 40)
            project_nearby = re.search(
                r"(?i)\bproject\b", text[window_start:window_end]
            )
            if not (all_caps or project_nearby):
                continue
            spans.append(
                _emit(
                    text, match.start(), match.end(), "business", "codename",
                    "proven", f"Looks like internal codename {name}.",
                    "policy word; ALL-CAPS or near 'Project'", policy, 0.9,
                )
            )
    for client in policy.clients:
        if not client.strip():
            continue
        for match in _phrase_pattern(client).finditer(text):
            spans.append(
                _emit(
                    text, match.start(), match.end(), "business", "client",
                    "proven", f"Looks like client name {client}.",
                    "policy word, case/hyphen-insensitive", policy, 0.9,
                )
            )
    for start, end in _sentences(text):
        body = text[start:end]
        lowered = body.lower()
        cue = next(
            (c for c in policy.figure_cues if c.lower() in lowered), None
        )
        if cue is None:
            continue
        for number in _NUMBER_RE.finditer(body):
            spans.append(
                _emit(
                    text,
                    start + number.start(),
                    start + number.end(),
                    "business", "figure", "proven",
                    "Looks like an unreleased figure.",
                    f"number near figure cue {cue!r}", policy, 0.8,
                )
            )
    return spans


def _scan_beacon(text: str, policy: Policy) -> list[Span]:
    spans: list[Span] = []
    for start, end in _sentences(text):
        body = text[start:end]
        noun = _SECRET_NOUN_RE.search(body)
        if noun is None:
            continue
        location = None
        for pattern in _LOCATION_RES:
            location = pattern.search(body)
            if location is not None:
                break
        if location is None:
            continue
        cp_start = start + min(noun.start(), location.start())
        cp_end = start + max(noun.end(), location.end())
        spans.append(
            _emit(
                text, cp_start, cp_end, "beacon", "beacon_cue", "ambiguous",
                "Points at where a secret lives.",
                f"secret noun {noun.group()!r} plus location "
                f"{location.group()!r} in one sentence",
                policy, 0.6,
            )
        )
    return spans


def scan(
    text: str, policy: Policy, registry: CanaryRegistry | None = None
) -> list[Span]:
    """Run every Tier 1 detector and return merged spans sorted by offset."""
    spans: list[Span] = []
    spans.extend(_scan_canaries(text, registry, policy))
    spans.extend(_scan_provider_keys(text, policy))
    spans.extend(_scan_pii(text, policy))
    spans.extend(_scan_business(text, policy))
    spans.extend(_scan_entropy(text, policy))
    spans.extend(_scan_beacon(text, policy))
    return merge_overlaps(spans)
