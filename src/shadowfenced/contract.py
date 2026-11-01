"""Contract-clause protection (T44): Tier 1 detector for pasted contract text.

``scan_contract_clauses`` finds clause-shaped spans: numbered or headed
clauses (``4.2 Termination``, ``Section 7 - Confidentiality``,
``Article III``), clauses carrying legal markers (shall, hereinafter,
indemnify, notwithstanding, governing law, liquidated damages), and
defined-party lines (``between Acme Ltd (the Supplier) and ...``).
Each clause becomes one proven ``business`` / ``contract_clause`` span,
so it redacts to a ``[CONTRACT_CLAUSE_n]`` placeholder like any other
PII placeholder and restores locally. Party names inside a clause keep
their normal handling: other detectors still run and overlapping proven
spans are carved out of the clause (see ``tier1.scan``), so both
placeholders survive.

Deliberately NOT clauses: a general legal question (``what does an
indemnity clause usually mean?``) and short quotes under
``MIN_MARKER_WORDS`` words with no clause number.

``confidential_marker_of`` reports handling-caveat stamps
(``Confidential``, ``Do not distribute``, ``Privileged``) used by the
gate to hold classified contracts. The defined term ``Confidential
Information`` and the concept ``confidentiality`` are not stamps: they
appear in ordinary NDA language, which redacts by default.
"""

from __future__ import annotations

import re

from .policy import Policy
from .spans import span_from_cp_range

SUBCLASS = "contract_clause"

#: Clauses built only on a legal marker need at least this many words;
#: shorter quotes without a clause number stay clean.
MIN_MARKER_WORDS = 12

#: Handling-caveat stamps. ``confidential`` excludes the defined term
#: ``Confidential Information`` (the thing being protected, not a
#: handling instruction).
CONFIDENTIAL_RE = re.compile(
    r"(?i)\bdo\s+not\s+distribute\b"
    r"|\bprivileged\b"
    r"|\bconfidential\b(?!\s+information\b)"
)

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?")

_ROMAN_RE = re.compile(
    r"^(?=[MDCLXVI])M*(C[MD]|D?C{0,3})(X[CL]|L?X{0,3})(I[XV]|V?I{0,3})$"
)

# Strong headings: keyword + number, dotted numbers, section marks.
_KEYWORD_HEADING_RE = re.compile(
    r"(?i)^(section|sec\.?|article|clause)[ \t]+"
    r"([IVXLCDM]+|\d+(?:\.\d+)*)(?![\w.])"
)
_DOTTED_NUMBER_RE = re.compile(r"^\d+\.\d+(?:\.\d+)*\b")
_SECTION_MARK_RE = re.compile(r"^[§¶][ \t]*\d+")
# Weak headings: a bare list-style number ("1. Services").
_WEAK_NUMBER_RE = re.compile(r"^\d+[.)}][ \t]+")

# Mid-line clause starts: a dotted number or a keyword heading followed
# by a capitalised title ("review 4.2 Termination", "see Section 7 -
# Confidentiality" is NOT one: lowercase "for" follows the number).
_MIDLINE_HEADING_RE = re.compile(
    r"(?<![\w.])"
    r"(?:\d+\.\d+(?:\.\d+)*[ \t]+[A-Z]"
    r"|(?:[Ss]ection|[Aa]rticle|[Cc]lause|[Ss]ec\.)[ \t]+"
    r"(?:\d+|[IVXLCDM]+)[ \t]*[-–—:.][ \t]*[A-Z])"
)

_LEGAL_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("shall", re.compile(r"(?i)\bshall\b")),
    ("hereinafter", re.compile(r"(?i)\bhereinafter\b")),
    ("indemnify", re.compile(r"(?i)\bindemnif\w*\b")),
    ("notwithstanding", re.compile(r"(?i)\bnotwithstanding\b")),
    ("governing law", re.compile(r"(?i)\bgoverning\s+law\b")),
    ("liquidated damages", re.compile(r"(?i)\bliquidated\s+damages\b")),
)

_DEFINED_PARTY_RE = re.compile(
    r"(?i)\bbetween\b[^.?!;]{0,300}?\(the\s+[A-Za-z][^()]{0,60}\)"
    r"[^.?!;]{0,120}?\band\b"
)

_QUESTION_START_RE = re.compile(
    r"(?i)^(what|why|how|when|where|which|who|whom|whose|can|could|would|"
    r"should|do|does|did|is|are|was|were|will|shall\s+i|explain|describe|"
    r"tell\s+me)\b"
)


def confidential_marker_of(text: str) -> str | None:
    """Name the handling-caveat stamp in text, if any.

    Returns ``"confidential"``, ``"do-not-distribute"`` or
    ``"privileged"`` (fixed vocabulary, never raw text), else None.
    """
    match = CONFIDENTIAL_RE.search(text)
    if match is None:
        return None
    hit = re.sub(r"\s+", " ", match.group()).strip().lower()
    if "distribute" in hit:
        return "do-not-distribute"
    if "privileg" in hit:
        return "privileged"
    return "confidential"


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def _is_question(chunk: str) -> bool:
    stripped = chunk.strip()
    return "?" in chunk or _QUESTION_START_RE.match(stripped) is not None


def _weak_heading(line: str) -> bool:
    return _WEAK_NUMBER_RE.match(line.strip()) is not None


def _first_marker(chunk: str) -> str | None:
    for name, pattern in _LEGAL_MARKERS:
        if pattern.search(chunk):
            return name
    return None


def _headed_kind(first_line: str) -> str | None:
    """Classify a chunk start: ``"keyword"``, ``"dotted"``, ``"weak"``.

    Keyword headings (``Section 7``, ``Article III``) and section marks
    are never version refs; dotted numbers (``4.2``) sometimes are.
    """
    text = first_line.strip()
    keyword = _KEYWORD_HEADING_RE.match(text)
    if keyword is not None:
        numeral = keyword.group(2).rstrip(".")
        if numeral[0].isdigit() or _ROMAN_RE.match(numeral.upper()):
            return "keyword"
    if _SECTION_MARK_RE.match(text) is not None:
        return "keyword"
    if _DOTTED_NUMBER_RE.match(text) is not None:
        return "dotted"
    if _weak_heading(first_line):
        return "weak"
    return None


def _qualify(chunk: str, first_line: str, midline: bool) -> str | None:
    """Return the marker kind that makes a chunk a clause, else None.

    A keyword heading always counts; a line-start dotted number counts
    too. Anything else needs a defined-party line or a legal marker
    with enough words (mid-line dotted numbers need substance, since
    they are often version refs). Questions never count.
    """
    stripped = chunk.strip()
    if not stripped or _word_count(stripped) < 2:
        return None
    headed = _headed_kind(first_line)
    if headed == "keyword" or (headed == "dotted" and not midline):
        return "clause heading with number"
    if _is_question(chunk):
        return None
    if _DEFINED_PARTY_RE.search(chunk):
        return "defined-party line"
    marker = _first_marker(chunk)
    if marker is None:
        return None
    words = _word_count(chunk)
    if words >= MIN_MARKER_WORDS:
        return f"legal marker '{marker}'"
    if (headed in ("weak", "dotted") or midline) and words >= 6:
        return f"legal marker '{marker}'"
    return None


def _paragraphs(text: str) -> list[tuple[str, int]]:
    """Split text into blank-line-separated ``(body, cp_start)`` parts."""
    parts: list[tuple[str, int]] = []
    cursor = 0
    for match in re.finditer(r"[ \t]*\n[ \t]*\n[ \t]*", text):
        if text[cursor:match.start()].strip():
            parts.append((text[cursor:match.start()], cursor))
        cursor = match.end()
    if text[cursor:].strip():
        parts.append((text[cursor:], cursor))
    return parts


def _split_points(paragraph: str) -> list[tuple[int, bool]]:
    """Chunk starts as ``(line-relative offset, midline)`` pairs.

    Besides headed-clause lines, a defined-party line and a question
    line each start their own chunk: the heading line above a parties
    line stays visible instead of being swallowed into the clause,
    and a trailing question (``Can you explain ...?``) stays visible
    instead of being glued to (and sinking) the last clause. When
    several rules fire at the same offset, the line start wins over a
    mid-line match so a numbered clause keeps its heading.
    """
    raw: list[tuple[int, bool]] = [(0, False)]
    cursor = 0
    for line in paragraph.split("\n"):
        stripped = line.strip()
        if cursor > 0:
            if _headed_kind(stripped) is not None:
                raw.append((cursor, False))
            if _DEFINED_PARTY_RE.search(stripped) is not None:
                raw.append((cursor, False))
            if _is_question(stripped):
                raw.append((cursor, False))
        for match in _MIDLINE_HEADING_RE.finditer(line):
            if cursor + match.start() > 0:
                raw.append((cursor + match.start(), True))
        cursor += len(line) + 1
    merged: dict[int, bool] = {}
    for offset, midline in raw:
        if offset in merged:
            merged[offset] = merged[offset] and midline
        else:
            merged[offset] = midline
    return sorted(merged.items())


def scan_contract_clauses(text: str, policy: Policy) -> list:
    """Find pasted contract clauses as proven business spans.

    One span per clause chunk: a headed clause always counts, an
    unheaded chunk counts on a defined-party line or a legal marker
    with enough words. Questions never count.
    """
    from .tier1 import _emit

    spans = []
    for paragraph, para_start in _paragraphs(text):
        points = _split_points(paragraph)
        for index, (start, midline) in enumerate(points):
            end = points[index + 1][0] if index + 1 < len(points) else len(paragraph)
            chunk = paragraph[start:end]
            if not chunk.strip():
                continue
            first_line = chunk.split("\n", 1)[0]
            why = _qualify(chunk, first_line, midline)
            if why is None:
                continue
            leading = len(chunk) - len(chunk.lstrip(" \t"))
            trailing = len(chunk) - len(chunk.rstrip(" \t\n"))
            cp_start = para_start + start + leading
            cp_end = para_start + end - trailing
            if cp_start >= cp_end or not text[cp_start:cp_end].strip():
                continue
            spans.append(
                _emit(
                    text,
                    cp_start,
                    cp_end,
                    "business",
                    SUBCLASS,
                    "proven",
                    "Looks like a pasted contract clause.",
                    why,
                    policy,
                    0.85,
                )
            )
    return spans
