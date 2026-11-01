"""Tier 1: deterministic code detectors (patterns, canaries, entropy, policy words).

``scan(text, policy, registry)`` returns a list of ``Span`` objects sorted by
offset. Proven spans (canaries, key shapes, PII, policy words) map to the
per-class policy action; ambiguous spans (entropy, beacon cues) suggest
``review``. Provider key prefixes are assembled at runtime from fragments so
no real-format literal ever appears in this file.
"""

from __future__ import annotations

import base64
import math
import re
import time
from collections import Counter
from typing import Any

from .canary import PREFIX as CANARY_PREFIX
from .redact import base_label
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

_CAP_TOKEN = r"[A-Z][a-z]+(?:[-'][A-Za-z]+)?"
_FIRST_TOKEN = r"(?:[A-Z][a-z]+|[a-z]+(?:[-'][a-z]+)?)"
_NAME_AFTER_CUE_RE = re.compile(
    r"(?i:\b(?:my\s+name\s+is|name\s*:|employee\s*:|customer\s*:"
    r"|patient\s*:|soy|me\s+llamo|dear|from\s*:))"
    r"\s*:?\s*(" + _FIRST_TOKEN + r"(?:[ \t]+" + _CAP_TOKEN + r"){1,3})\b"
)

# Header cell (normalised: lower, spaces/underscores removed) -> PII type.
_CSV_COLUMNS = {
    "name": "name",
    "firstname": "name",
    "lastname": "name",
    "fullname": "name",
    "email": "email",
    "phone": "phone",
    "mobile": "phone",
    "dob": "dob",
    "birth": "dob",
    "address": "address",
    "ssn": "ssn",
}

_DOB_CUE_RE = re.compile(
    r"(?i)\b(d\.?\s*o\.?\s*b\.?|born|birth|fecha\s+de\s+nacimiento)\b"
)
_DOB_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})"
    r"|\d{1,2}[./-]\d{1,2}[./-](?:\d{4}|\d{2}))\b"
)

_ADDR_SUFFIX = (
    r"St|Street|Ave|Avenue|Rd|Road|Way|Lane|Ln|Dr|Drive|Blvd|Ct|Court"
    r"|Terrace|Ter|Place|Pl|Circle"
)
_ADDR_RE = re.compile(
    r"\b\d{1,5}\s+[A-Za-z0-9'-]+(?:\s+[A-Za-z0-9'-]+){0,3}\s+"
    r"(?:" + _ADDR_SUFFIX + r")\b\.?"
    r"(?:\s*,?\s*(?:Apt|Unit|Suite)\s*#?\s*[A-Za-z0-9-]+)?"
    r"(?:\s*,?\s*[A-Za-z]+(?:\s+[A-Za-z]+)?,?\s+[A-Z]{2}\s+\d{5}"
    r"(?:-\d{4})?)?",
    re.IGNORECASE,
)
_ADDR_EU_RE = re.compile(
    r"\b(?:Calle|Avenida|Rue|Via)\s+[A-Za-z'-]+"
    r"(?:\s+[A-Za-z'-]+){0,2}\s+\d{1,4}[A-Za-z]?\b",
    re.IGNORECASE,
)

_SHORT_PHONE_RE = re.compile(r"(?<!\d)\d{3}[-. ]\d{4}(?!\d)")
_PHONE_CUE_RE = re.compile(
    r"(?i)\b(phone|mobile|tel|telephone|contact|call|fax)\b"
)

_FILE_MAGICS = (
    b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"%PDF", b"PK\x03\x04",
)

_PASSWORD_PLACEHOLDER_RE = re.compile(
    r"(?i)^(?:changeme|example|test|secret|password|todo|replace_me)"
    r"[-_#0-9]*$|^x{2,}$|^\*+$|^<.*>$|^\$\{.*\}$|^\{\{.*\}\}$|^your_"
)

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


def _password_value_placeholder(match_text: str) -> bool:
    """True when a password-assignment value is a well-known placeholder."""
    found = re.search(r"[:=]\s*(\S+)", match_text)
    if found is None:
        return False
    value = found.group(1).strip().strip("\"'").rstrip(",.;!?")
    if not value:
        return True
    return _PASSWORD_PLACEHOLDER_RE.search(value) is not None


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
        if _password_value_placeholder(match.group()):
            continue
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


def _decodes_to_file(token: str) -> bool:
    """True when a base64 token decodes to a known file header."""
    if "-" in token or "_" in token:
        return False
    if re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", token) is None:
        return False
    try:
        # The entropy scanner strips trailing punctuation (including
        # base64 padding), so re-pad before decoding.
        padded = token + "=" * (-len(token) % 4)
        raw = base64.b64decode(padded, validate=True)
    except Exception:
        return False
    return raw.startswith(_FILE_MAGICS)


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
        if _decodes_to_file(token):
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


def _csv_header_map(header_line: str) -> list[str | None] | None:
    """Map a CSV header line to PII types, or None when not a header."""
    cells = [cell.strip().strip("\"'") for cell in header_line.split(",")]
    if len(cells) < 2:
        return None
    normalised = [re.sub(r"[\s_]+", "", cell.lower()) for cell in cells]
    if not any(cell in _CSV_COLUMNS for cell in normalised):
        return None
    return [_CSV_COLUMNS.get(cell) for cell in normalised]


def _csv_tables(text: str) -> list[dict]:
    """Find CSV tables: header row plus contiguous comma data rows."""
    lines = text.split("\n")
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line) + 1
    tables = []
    index = 0
    while index < len(lines):
        colmap = _csv_header_map(lines[index])
        if colmap is None:
            index += 1
            continue
        rows: list[int] = []
        next_index = index + 1
        while (
            next_index < len(lines)
            and lines[next_index].strip()
            and "," in lines[next_index]
        ):
            rows.append(next_index)
            next_index += 1
        if rows:
            tables.append({"colmap": colmap, "rows": rows})
        index = next_index
    return [
        {**table, "lines": lines, "offsets": offsets} for table in tables
    ]


def _csv_cell_span(
    table: dict, row: int, col: int, text: str, policy: Policy,
    subtype: str,
) -> Span | None:
    """Build a proven PII span for one CSV cell value."""
    cells = table["lines"][row].split(",")
    if col >= len(cells):
        return None
    cell = cells[col]
    value = cell.strip().strip("\"'")
    if not value:
        return None
    line_start = table["offsets"][row]
    cursor = 0
    for pos, raw in enumerate(cells):
        if pos == col:
            break
        cursor += len(raw) + 1
    stripped_lead = len(cell) - len(cell.lstrip())
    stripped_trail = len(cell) - len(cell.rstrip())
    quote_lead = 1 if cell.strip()[:1] in "\"'" else 0
    quote_trail = 1 if cell.strip()[-1:] in "\"'" and len(value) else 0
    cp_start = line_start + cursor + stripped_lead + quote_lead
    cp_end = (
        line_start + cursor + len(cell) - stripped_trail - quote_trail
    )
    if cp_start >= cp_end or text[cp_start:cp_end] != value:
        # Fall back to locating the value inside the line.
        found = table["lines"][row].find(value)
        if found < 0:
            return None
        cp_start = line_start + found
        cp_end = cp_start + len(value)
    return _emit(
        text, cp_start, cp_end, "pii", subtype, "proven",
        "Looks like personal data in a labelled CSV column.",
        f"value under a {subtype} header column", policy, 0.85,
    )


def _scan_csv_pii(text: str, policy: Policy) -> list[Span]:
    """Treat values under sensitive CSV header columns as PII."""
    spans: list[Span] = []
    for table in _csv_tables(text):
        for row in table["rows"]:
            for col, subtype in enumerate(table["colmap"]):
                if subtype is None:
                    continue
                span = _csv_cell_span(table, row, col, text, policy, subtype)
                if span is not None:
                    spans.append(span)
    return spans


def _scan_names(text: str, policy: Policy) -> list[Span]:
    """Person names appearing right after an introducing cue."""
    spans: list[Span] = []
    for match in _NAME_AFTER_CUE_RE.finditer(text):
        start = match.start(1)
        spans.append(
            _emit(
                text, start, start + len(match.group(1)), "pii", "name",
                "proven", "Looks like a person name.",
                "2-4 capitalised tokens after a name cue", policy, 0.85,
            )
        )
    return spans


def _scan_dob(text: str, policy: Policy) -> list[Span]:
    """ISO/US/EU dates near a date-of-birth cue."""
    cues = list(_DOB_CUE_RE.finditer(text))
    if not cues:
        return []
    spans: list[Span] = []
    for match in _DOB_DATE_RE.finditer(text):
        near = any(
            (cue.end() <= match.start() and match.start() - cue.end() <= 25)
            or (cue.start() >= match.end() and cue.start() - match.end() <= 25)
            for cue in cues
        )
        if near:
            spans.append(
                _emit(
                    text, match.start(), match.end(), "pii", "dob", "proven",
                    "Looks like a date of birth.",
                    "calendar date within 25 chars of a birth cue",
                    policy, 0.85,
                )
            )
    return spans


def _scan_addresses(text: str, policy: Policy) -> list[Span]:
    """Street addresses (local and European forms)."""
    spans: list[Span] = []
    for match in _ADDR_RE.finditer(text):
        spans.append(
            _emit(
                text, match.start(), match.end(), "pii", "address", "proven",
                "Looks like a street address.",
                "house number plus street suffix", policy, 0.85,
            )
        )
    for match in _ADDR_EU_RE.finditer(text):
        spans.append(
            _emit(
                text, match.start(), match.end(), "pii", "address", "proven",
                "Looks like a street address.",
                "european street form (Calle/Avenida/Rue/Via)",
                policy, 0.85,
            )
        )
    return spans


def _scan_short_phones(
    text: str, policy: Policy, tables: list[dict],
) -> list[Span]:
    """7-digit local numbers, only in a phone/CSV context."""
    phone_table_rows: set[tuple[int, int]] = set()
    for table in tables:
        if "phone" in (table["colmap"] or []):
            for row in table["rows"]:
                line_start = table["offsets"][row]
                line = table["lines"][row]
                phone_table_rows.add((line_start, line_start + len(line)))
    spans: list[Span] = []
    for match in _SHORT_PHONE_RE.finditer(text):
        window_start = max(0, match.start() - 40)
        window_end = min(len(text), match.end() + 40)
        in_csv = any(
            lo <= match.start() and match.end() <= hi
            for lo, hi in phone_table_rows
        )
        if in_csv or _PHONE_CUE_RE.search(text[window_start:window_end]):
            spans.append(
                _emit(
                    text, match.start(), match.end(), "pii", "phone",
                    "proven", "Looks like a phone number.",
                    "7-digit number in a phone context", policy, 0.8,
                )
            )
    return spans


# --- T29: financial account details ---------------------------------------

# Labels that mark a nearby digit run as a bank account number. Bare
# "account" is included so "account 12345678" (e.g. after a sort code)
# is caught; a number must still follow within the search window.
_ACCOUNT_LABEL_RE = re.compile(
    r"(?i)\b(account\s*(?:number|no\.?)|acct(?:\.|\s*no\.?)?|a/c"
    r"|checking|savings|account)\b"
)
# Masked forms such as ****8841: mask chars plus the last 3-4 digits.
# Masked forms are low sensitivity (proven pii, redacted like the rest).
_ACCOUNT_MASKED_RE = re.compile(r"[*xX#\u2022]{2,}[ \t-]*\d{3,4}")
_ACCOUNT_PLAIN_RE = re.compile(r"(?<!\d)\d(?:[ \t-]?\d){5,16}(?!\d)")

_ROUTING_LABEL_RE = re.compile(r"(?i)\b(routing(?:\s+number)?|ABA|RTN)\b")
_ROUTING_NUM_RE = re.compile(r"(?<!\d)\d{9}(?!\d)")

# An IBAN start (country code + check digits); the BBAN run is consumed
# by _iban_end() so spaced groups ("GB82 WEST 1234 ...") work too.
_IBAN_START_RE = re.compile(r"\b[A-Z]{2}\d{2}")

_SORT_LABEL_RE = re.compile(r"(?i)\bsort\s*code\b")
_SORT_NUM_RE = re.compile(r"(?<!\d)\d{2}[-\s]?\d{2}[-\s]?\d{2}(?!\d)")

_SWIFT_LABEL_RE = re.compile(r"(?i)\b(SWIFT|BIC)\b")
_SWIFT_CODE_RE = re.compile(r"\b[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")

# "Account holder:", "Name:", "Patient:", "Employee:", "Cardholder:".
# A colon is required so prose mentions ("patients table", "account
# team") never match.
_HOLDER_CUE_RE = re.compile(
    r"(?i)\b(account\s*holder|card\s*holder|cardholder|holder"
    r"|patient|employee|full\s*name|contact\s*name|name)\s*:"
    r"[ \t]*(" + _FIRST_TOKEN + r"(?:[ \t]+" + _CAP_TOKEN + r"){1,3})\b"
)

_LABEL_WINDOW = 60


def _aba_valid(digits: str) -> bool:
    """ABA routing-number checksum: 3-7-1 weighting sums to a multiple of 10."""
    values = [ord(ch) - ord("0") for ch in digits]
    total = (
        3 * (values[0] + values[3] + values[6])
        + 7 * (values[1] + values[4] + values[7])
        + (values[2] + values[5] + values[8])
    )
    return total % 10 == 0


def _iban_mod97_ok(iban: str) -> bool:
    """ISO 13616 mod-97 check: rearranged digits must leave remainder 1."""
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(
        str(ord(ch) - 55) if "A" <= ch <= "Z" else ch for ch in rearranged
    )
    return int(numeric) % 97 == 1


def _first_after(
    text: str, label: re.Match[str], pattern: re.Pattern[str],
) -> re.Match[str] | None:
    """First ``pattern`` match in the window after a label."""
    return pattern.search(
        text, label.end(), min(len(text), label.end() + _LABEL_WINDOW))


def _last_before(
    text: str, label: re.Match[str], pattern: re.Pattern[str],
) -> re.Match[str] | None:
    """Nearest ``pattern`` match in the window before a label."""
    window_start = max(0, label.start() - _LABEL_WINDOW)
    before = None
    for candidate in pattern.finditer(text, window_start, label.start()):
        before = candidate
    return before


def _near_number(
    text: str, label: re.Match[str], pattern: re.Pattern[str],
) -> re.Match[str] | None:
    """First ``pattern`` match near a label: after it first, else before it."""
    after = _first_after(text, label, pattern)
    if after is not None:
        return after
    return _last_before(text, label, pattern)


def _scan_accounts(text: str, policy: Policy) -> list[Span]:
    """Bank account numbers (6-17 digits, or masked) next to an account label."""
    spans: list[Span] = []
    for label in _ACCOUNT_LABEL_RE.finditer(text):
        # Prefer the window after the label (either form, whichever is
        # nearer); only then look before it ("8841 is the account number").
        # Checking both forms in each window stops an earlier label's
        # masked number from hiding this label's plain number.
        after = [
            match for match in (
                _first_after(text, label, _ACCOUNT_MASKED_RE),
                _first_after(text, label, _ACCOUNT_PLAIN_RE),
            ) if match is not None
        ]
        before = [] if after else [
            match for match in (
                _last_before(text, label, _ACCOUNT_MASKED_RE),
                _last_before(text, label, _ACCOUNT_PLAIN_RE),
            ) if match is not None
        ]
        found = (
            min(after or before, key=lambda m: m.start(), default=None)
            if after else
            max(before, key=lambda m: m.start(), default=None)
        )
        if found is None:
            continue
        digits = re.sub(r"[ \t-]", "", found.group())
        masked = not digits.isdigit()
        if not masked and not 6 <= len(digits) <= 17:
            continue
        spans.append(
            _emit(
                text, found.start(), found.end(), "pii", "account",
                "proven", "Looks like a bank account number.",
                "masked digits near an account label" if masked
                else "6-17 digits near an account label",
                policy, 0.8 if masked else 0.85,
            )
        )
    return spans


def _scan_routing(text: str, policy: Policy) -> list[Span]:
    """US ABA routing numbers: 9 digits by a routing/ABA/RTN label, checksum valid."""
    spans: list[Span] = []
    for label in _ROUTING_LABEL_RE.finditer(text):
        for candidate in _ROUTING_NUM_RE.finditer(
            text,
            max(0, label.start() - _LABEL_WINDOW),
            min(len(text), label.end() + _LABEL_WINDOW),
        ):
            if _aba_valid(candidate.group()):
                spans.append(
                    _emit(
                        text, candidate.start(), candidate.end(), "pii",
                        "routing", "proven", "Looks like a bank routing number.",
                        "9 digits by a routing label passing the ABA checksum",
                        policy, 0.9,
                    )
                )
    return spans


def _iban_end(text: str, start: int) -> int:
    """End offset of the BBAN run starting at ``start`` (single spaces allowed)."""
    pos = start
    limit = min(len(text), start + 45)
    while pos < limit and (text[pos].isalnum() or text[pos] == " "):
        pos += 1
    while pos > start and text[pos - 1] == " ":
        pos -= 1
    return pos


def _scan_iban(text: str, policy: Policy) -> list[Span]:
    """IBANs passing the ISO 13616 mod-97 check, plus UK sort-code pairs."""
    spans: list[Span] = []
    for start_match in _IBAN_START_RE.finditer(text):
        start = start_match.start()
        run_end = _iban_end(text, start_match.end())
        stripped = re.sub(r"\s+", "", text[start:run_end])
        longest = min(len(stripped), 34)
        for length in range(longest, 14, -1):
            candidate = stripped[:length]
            if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", candidate):
                continue
            end = start
            kept = 0
            while kept < length:
                if text[end] != " ":
                    kept += 1
                end += 1
            if end < len(text) and text[end].isalnum():
                continue
            if _iban_mod97_ok(candidate):
                spans.append(
                    _emit(
                        text, start, end, "pii", "iban", "proven",
                        "Looks like an IBAN.",
                        "country code plus check digits passing the "
                        "ISO 13616 mod-97 check", policy, 0.9,
                    )
                )
                break
    for label in _SORT_LABEL_RE.finditer(text):
        number = _near_number(text, label, _SORT_NUM_RE)
        if number is not None:
            spans.append(
                _emit(
                    text, number.start(), number.end(), "pii", "sort_code",
                    "proven", "Looks like a UK sort code.",
                    "6 digits by a sort-code label", policy, 0.85,
                )
            )
    return spans


def _scan_swift(text: str, policy: Policy) -> list[Span]:
    """SWIFT/BIC codes (8 or 11 chars) next to a SWIFT/BIC label."""
    spans: list[Span] = []
    for label in _SWIFT_LABEL_RE.finditer(text):
        code = _near_number(text, label, _SWIFT_CODE_RE)
        if code is not None:
            spans.append(
                _emit(
                    text, code.start(), code.end(), "pii", "swift", "proven",
                    "Looks like a SWIFT/BIC code.",
                    "8 or 11 characters by a SWIFT/BIC label", policy, 0.9,
                )
            )
    return spans


def _scan_holder_names(text: str, policy: Policy) -> list[Span]:
    """Account holder / patient / employee names after a holder label."""
    spans: list[Span] = []
    for match in _HOLDER_CUE_RE.finditer(text):
        start = match.start(2)
        spans.append(
            _emit(
                text, start, start + len(match.group(2)), "pii",
                "holder_name", "proven",
                "Looks like an account holder name.",
                "2-4 capitalised tokens after a holder label", policy, 0.85,
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


def _damerau_levenshtein(a: str, b: str) -> int:
    """Damerau-Levenshtein distance for short tokens (optimal alignment)."""
    if a == b:
        return 0
    len_a, len_b = len(a), len(b)
    if abs(len_a - len_b) > 1:
        return 2
    prev = list(range(len_b + 1))
    prev_prev = prev[:]
    for i in range(1, len_a + 1):
        current = [i] + [0] * len_b
        for j in range(1, len_b + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(
                prev[j] + 1, current[j - 1] + 1, prev[j - 1] + cost
            )
            if (
                i > 1
                and j > 1
                and a[i - 1] == b[j - 2]
                and a[i - 2] == b[j - 1]
            ):
                current[j] = min(current[j], prev_prev[j - 2] + 1)
        prev_prev = prev
        prev = current
    return prev[len_b]


def _client_token_match(client_token: str, text_token: str) -> bool:
    """Exact match, or one typo when the client token has >= 6 letters."""
    if client_token.lower() == text_token.lower():
        return True
    letters = re.sub(r"[^A-Za-z]", "", client_token)
    if len(letters) < 6:
        return False
    return _damerau_levenshtein(client_token.lower(), text_token.lower()) <= 1


def _scan_clients_fuzzy(
    text: str, policy: Policy, words: list[tuple[int, int, str]],
) -> list[Span]:
    """Client names with one typo tolerated per long token."""
    spans: list[Span] = []
    for client in policy.clients:
        if not client.strip():
            continue
        wanted = re.findall(r"[A-Za-z]+", client)
        if not wanted:
            continue
        for index in range(len(words) - len(wanted) + 1):
            window = words[index:index + len(wanted)]
            if not all(
                _client_token_match(w, word)
                for w, (_, _, word) in zip(wanted, window)
            ):
                continue
            gap_ok = all(
                re.fullmatch(r"[\s-]+", text[window[k][1]:window[k + 1][0]])
                for k in range(len(window) - 1)
            )
            if not gap_ok:
                continue
            spans.append(
                _emit(
                    text, window[0][0], window[-1][1], "business", "client",
                    "proven", f"Looks like client name {client}.",
                    "policy word, case/hyphen-insensitive, one typo allowed",
                    policy, 0.85,
                )
            )
    return spans


_BIZ_CONTEXT_RE = re.compile(
    r"(?i)\b(integration|launch|roadmap|slipped|delay|release|margin"
    r"|forecast|client|deal|contract|migration)\b"
)
_COOK_WORD_RE = re.compile(
    r"(?i)\b(recipes?|candles?|soaps?|melts?|melted|melting|grams?|cups?|oz)\b"
)
_FIGURE_AMOUNT_RE = re.compile(
    r"(?i)(?:\$\s*\d[\d,]*(?:\.\d+)?(?:\s?(?:million|billion|bn|m|k))?"
    r"|\b\d+(?:\.\d+)?\s?(?:%|million|billion|bn|m|k))(?![A-Za-z0-9])"
)


def _scan_business(text: str, policy: Policy) -> list[Span]:
    spans: list[Span] = []
    sentences = _sentences(text)
    for name in policy.codename_names():
        for match in _codename_pattern(name).finditer(text):
            surface = match.group()
            all_caps = surface == surface.upper() and re.search(r"[A-Z]", surface)
            window_start = max(0, match.start() - 40)
            window_end = min(len(text), match.end() + 40)
            project_nearby = re.search(
                r"(?i)\bproject\b", text[window_start:window_end]
            )
            context_start = max(0, match.start() - 60)
            context_end = min(len(text), match.end() + 60)
            context_nearby = _BIZ_CONTEXT_RE.search(
                text[context_start:context_end]
            )
            if not (all_caps or project_nearby or context_nearby):
                continue
            sentence = next(
                (text[s:e] for s, e in sentences
                 if s <= match.start() < e),
                "",
            )
            if _COOK_WORD_RE.search(sentence):
                continue
            spans.append(
                _emit(
                    text, match.start(), match.end(), "business", "codename",
                    "proven", f"Looks like internal codename {name}.",
                    "policy word; ALL-CAPS, near 'Project', or near a "
                    "business context word", policy, 0.9,
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
    words = [
        (m.start(), m.end(), m.group()) for m in re.finditer(r"[A-Za-z]+", text)
    ]
    spans.extend(_scan_clients_fuzzy(text, policy, words))
    for start, end in sentences:
        body = text[start:end]
        lowered = body.lower()
        cue = next(
            (c for c in policy.figure_cues if c.lower() in lowered), None
        )
        if cue is not None:
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
    business_ranges = [
        (s.cp_start, s.cp_end) for s in spans if s.cls == "business"
    ]
    if business_ranges:
        for start, end in sentences:
            if not any(bs < end and start < be for bs, be in business_ranges):
                continue
            for amount in _FIGURE_AMOUNT_RE.finditer(text[start:end]):
                spans.append(
                    _emit(
                        text,
                        start + amount.start(),
                        start + amount.end(),
                        "business", "figure", "proven",
                        "Looks like an unreleased figure.",
                        "percentage or money/volume amount in a sentence "
                        "with a business name", policy, 0.8,
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


_CUSTOM_SUBCLASS = {
    "codename": "codename",
    "client": "client",
    "pattern": "custom_pattern",
}


def _scan_custom_rules(text: str, policy: Policy) -> list[Span]:
    """User organisation rules as Tier 1 proven spans with the chosen action.

    Codename and client rules match literally (case-insensitively, like the
    base policy words); pattern rules match as user-supplied regular
    expressions. Every hit is ``proven`` with ``proof="custom"`` and carries
    the rule's own action, so the gate can honour block/review directly.
    """
    rules = list(getattr(policy, "custom_rules", None) or [])
    if not text or not rules:
        return []
    spans: list[Span] = []
    for rule in rules:
        kind = getattr(rule, "kind", "")
        value = getattr(rule, "value", "") or ""
        action = getattr(rule, "action", "")
        subclass = _CUSTOM_SUBCLASS.get(kind)
        if subclass is None or not value.strip():
            continue
        if action not in ("block", "redact", "review"):
            continue
        try:
            if kind == "codename":
                pattern = re.compile(
                    r"\b" + re.escape(value.strip()) + r"\b", re.IGNORECASE
                )
            elif kind == "client":
                pattern = _phrase_pattern(value.strip())
            else:
                pattern = re.compile(value)
        except re.error:
            continue
        for match in pattern.finditer(text):
            if match.start() == match.end():
                continue
            spans.append(
                span_from_cp_range(
                    text,
                    match.start(),
                    match.end(),
                    cls="business",
                    subclass=subclass,
                    tier=1,
                    proof="custom",
                    status="proven",
                    message="Matched a custom organisation rule.",
                    why=f"custom {kind} rule with action {action}",
                    suggested_action=action,
                    can_keep=policy.keepable("business"),  # type: ignore[arg-type]
                    confidence=0.9,
                )
            )
    return spans


def _trace_labels(
    found: list[Span], counters: dict[str, int]
) -> list[str]:
    """Placeholder-style labels for trace steps; never raw values."""
    labels: list[str] = []
    for span in found:
        base = base_label(span.cls, span.subclass)
        counters[base] = counters.get(base, 0) + 1
        labels.append(f"[{base}_{counters[base]}]")
    return labels


def _run_traced(
    trace_out: list[dict[str, Any]] | None,
    spans: list[Span],
    step: str,
    counters: dict[str, int],
    func: Any,
    *args: Any,
) -> None:
    """Run one Tier 1 detector, timing it into the trace when asked."""
    if trace_out is None:
        spans.extend(func(*args))
        return
    started = time.perf_counter()
    found = func(*args)
    elapsed_ms = (time.perf_counter() - started) * 1000
    spans.extend(found)
    if found:
        detail = (
            f"{len(found)} hit(s): "
            + ", ".join(_trace_labels(found, counters))
        )
        result = "hit"
    else:
        detail = "0 hits"
        result = "clean"
    trace_out.append(
        {
            "step": step,
            "tool": "tier1",
            "detail": detail,
            "result": result,
            "ms": round(elapsed_ms, 3),
        }
    )


def scan_reid(
    text: str, trace_out: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Extract re-identification quasi-identifiers (Tier 1 code, no model).

    Thin hook over :mod:`shadowfenced.reid`: returns
    ``[{kind, span, value}]`` findings and appends the ``reid.extract``
    trace step (counts, kind labels and fingerprints, never raw values)
    when ``trace_out`` is a list. Adds no spans and changes no verdicts.
    """
    from . import reid as reid_module

    findings = reid_module.extract_quasi_identifiers(text)
    if trace_out is not None:
        reid_module.trace_extract(trace_out, findings)
    return findings


def scan(
    text: str, policy: Policy, registry: CanaryRegistry | None = None,
    trace_out: list[dict[str, Any]] | None = None
) -> list[Span]:
    """Run every Tier 1 detector and return merged spans sorted by offset.

    When ``trace_out`` is a list, one ``{step, tool, detail, result, ms}``
    entry per detector is appended: counts and placeholder-style labels
    only, never raw text.
    """
    spans: list[Span] = []
    counters: dict[str, int] = {}
    _run_traced(trace_out, spans, "tier1.canary_scan", counters, _scan_canaries, text, registry, policy)
    _run_traced(trace_out, spans, "tier1.key_scan", counters, _scan_provider_keys, text, policy)
    _run_traced(trace_out, spans, "tier1.pii_scan", counters, _scan_pii, text, policy)
    _run_traced(trace_out, spans, "tier1.name_scan", counters, _scan_names, text, policy)
    _run_traced(trace_out, spans, "tier1.csv_scan", counters, _scan_csv_pii, text, policy)
    _run_traced(trace_out, spans, "tier1.dob_scan", counters, _scan_dob, text, policy)
    _run_traced(trace_out, spans, "tier1.address_scan", counters, _scan_addresses, text, policy)
    _run_traced(trace_out, spans, "tier1.account_scan", counters, _scan_accounts, text, policy)
    _run_traced(trace_out, spans, "tier1.routing_scan", counters, _scan_routing, text, policy)
    _run_traced(trace_out, spans, "tier1.iban_scan", counters, _scan_iban, text, policy)
    _run_traced(trace_out, spans, "tier1.swift_scan", counters, _scan_swift, text, policy)
    _run_traced(trace_out, spans, "tier1.holder_scan", counters, _scan_holder_names, text, policy)
    tables = _csv_tables(text)
    _run_traced(trace_out, spans, "tier1.short_phone_scan", counters, _scan_short_phones, text, policy, tables)
    _run_traced(trace_out, spans, "tier1.business_scan", counters, _scan_business, text, policy)
    _run_traced(trace_out, spans, "tier1.custom_scan", counters, _scan_custom_rules, text, policy)
    _run_traced(trace_out, spans, "tier1.entropy_scan", counters, _scan_entropy, text, policy)
    _run_traced(trace_out, spans, "tier1.beacon_scan", counters, _scan_beacon, text, policy)
    return merge_overlaps(spans)
