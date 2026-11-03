"""Extension file guard (T67): no file reaches an AI site unchecked.

Owner principle: better slow than leaked — a file the guard has not
cleared must never reach the site. The browser extension intercepts
every attach/drop/paste, posts the bytes here, and only lets a file
through once this module clears it (or the user explicitly overrides
a personal-data-only finding).

Pipeline (all local, all code except the optional deep check):

- :func:`scan_upload` extracts text with the existing
  :mod:`extract` / :mod:`image_redact` helpers, runs the Tier 1 code
  detectors plus the code-only gate over the full text
  (:func:`engine.gate` in ``tier1`` mode with model judges disabled),
  and maps the verdict onto ``CLEAN`` | ``BLOCK`` | ``UNSCANNABLE``.
- :func:`build_redacted_copy` sanitises the stored text until the same
  gate says ``ALLOW``, so the copy it returns always scans ``CLEAN``.
- :func:`start_deep_check` / :func:`get_deep_check` run an optional
  Muse Glimmer pass over the text in chunks. The deep check only adds
  findings; it can never clear a code finding.

Every payload carries labels and counts only — never raw values. Scan
records and deep-check jobs live in memory with a 15 minute TTL, like
the upload store and the placeholder maps.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .engine import PLACEHOLDER_DESCRIPTIONS
from .redact import base_label, redact

log = logging.getLogger("shadowfenced.fileguard")

#: Hard cap for one guarded file (spec: 25 MB). Anything bigger is
#: UNSCANNABLE, never an error: the file stays blocked.
EXT_SCAN_MAX_BYTES = 25 * 1024 * 1024

#: Scan records and deep-check jobs expire after 15 minutes.
SCAN_TTL_S = 15 * 60

#: Text-like suffixes: decoded as UTF-8 and redacted in place, keeping
#: their format. ``.env`` files are covered both by suffix and by name.
TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".log", ".env", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".properties", ".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts",
    ".tsx", ".mts", ".cts", ".java", ".c", ".h", ".cpp", ".cc", ".cxx",
    ".hpp", ".hxx", ".cs", ".go", ".rs", ".rb", ".php", ".pl", ".pm",
    ".swift", ".kt", ".kts", ".scala", ".sh", ".bash", ".zsh", ".ps1",
    ".bat", ".cmd", ".sql", ".r", ".lua", ".xml", ".html", ".htm",
    ".css", ".scss", ".vue",
})

#: Media types for redacted text copies (default: text/plain).
_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/json",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
}

#: Token scrubbed ranges collapse to (keeps line layout readable).
SCRUB_TOKEN = "[… removed]"

#: Note attached when a DOCX/PDF becomes plain text.
LAYOUT_NOTE = "Saved as text — the layout isn't kept"

#: Passes of (redact spans, scrub re-id ranges, scrub cue words) before
#: the sanitiser gives up and refuses to release the copy.
MAX_SANITIZE_PASSES = 4

#: Deep check reads the text in fixed chunks through local Glimmer.
DEEP_CHECK_CHUNK_CHARS = 4000
#: Upper bound so a 25 MB file cannot queue hours of model time; the
#: job reports ``truncated`` when the text runs past the cap.
DEEP_CHECK_MAX_CHUNKS = 25
#: Measured Glimmer throughput on this PC's judge model
#: (``muse-glimmer:30b`` via Ollama, timed with
#: ``scripts/time_glimmer.py`` span-judge calls): one 4000-char chunk
#: with a short JSON verdict takes about 12 s end to end.
DEEP_CHECK_SEC_PER_CHUNK = 12.0
#: Model wake-up plus first-token overhead, measured the same way.
DEEP_CHECK_SETUP_S = 3.0

#: Labels the deep-check judge may return (fixed vocabulary, matched
#: case-insensitively; anything else collapses to "sensitive detail").
DEEP_CHECK_LABELS = frozenset({
    "email address",
    "phone number",
    "person name",
    "street address",
    "date of birth",
    "account number",
    "secret",
    "password",
    "api key",
    "canary token",
})

_PLURALS = {
    "email address": "email addresses",
}

#: Friendlier chip labels for bases the engine leaves generic.
_NICE_LABELS = {
    "PII_NAME": "person name",
    "PII_ADDRESS": "street address",
    "PII_DOB": "date of birth",
    "PII_SSN": "identity number",
}

#: PII subclasses that count as financial identifiers: files holding any
#: of these can never offer "Upload anyway" (only a redacted copy).
_FINANCIAL_SUBCLASSES = frozenset({
    "account", "sort_code", "card", "iban", "routing", "swift",
})


def _plural(count: int, singular: str) -> str:
    if count == 1:
        return f"1 {singular}"
    return f"{count} {_PLURALS.get(singular, singular + 's')}"


_PROVIDER_MESSAGE_RE = re.compile(r"^An?\s+(.+?)\s+API key\s+[—-]\s+not sent\s*$")


class FileguardError(RuntimeError):
    """A guarded-file operation failed closed (never leaks values)."""

    def __init__(self, message: str, *, decision: str = "UNSCANNABLE") -> None:
        super().__init__(message)
        self.decision = decision
        self.why = message


class UnknownScan(FileguardError):
    """The scan id is unknown or expired."""

    def __init__(self) -> None:
        super().__init__("unknown or expired scan id", decision="UNKNOWN")


class RedactRefused(FileguardError):
    """No redacted copy exists for this scan (clean or unscannable)."""


@dataclass
class ScanRecord:
    """One guarded file's extracted text and verdict (memory only)."""

    scan_id: str
    name: str
    kind: str
    text: str
    sha256: str
    decision: str  # CLEAN | BLOCK | UNSCANNABLE
    why: str
    kinds: list[dict[str, Any]] = field(default_factory=list)
    has_secret: bool = False
    identity_bundle: bool = False
    has_financial: bool = False
    overridable: bool = False
    pages: int = 0
    chars: int = 0
    code_ms: float = 0.0
    image_png: bytes | None = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + SCAN_TTL_S)


class ScanStore:
    """Thread-safe in-memory scan registry."""

    def __init__(self) -> None:
        self._scans: dict[str, ScanRecord] = {}
        self._lock = threading.Lock()

    def save(self, record: ScanRecord) -> ScanRecord:
        with self._lock:
            self._scans[record.scan_id] = record
        return record

    def load(self, scan_id: str) -> ScanRecord | None:
        with self._lock:
            record = self._scans.get(scan_id or "")
            if record is None:
                return None
            if time.time() > record.expires_at:
                del self._scans[scan_id]
                return None
            return record

    def clear(self) -> None:
        with self._lock:
            self._scans.clear()


SCANS = ScanStore()


def reset_for_tests() -> None:
    """Drop every scan record and deep-check job (tests only)."""
    SCANS.clear()
    with _JOBS_LOCK:
        _JOBS.clear()


def _code_gate(text: str, policy: Any, registry: Any) -> Any:
    """Code-only gate: Tier 1 plus code re-id, never a model call.

    ``tier1`` mode with the person-search judge replaced by a stub, so
    only the deterministic code fallback can fire. Fast enough to run
    over a whole document on every attach.
    """
    from . import engine as engine_module

    return engine_module.gate(
        text, policy, registry, mode="tier1",
        person_search_judge_fn=lambda _text: None,
    )


def span_label(span: Any) -> str:
    """Plain-words label for one Tier 1 span (never its value)."""
    if span.cls == "secret" and span.subclass == "provider_key":
        match = _PROVIDER_MESSAGE_RE.match(span.message or "")
        if match:
            provider = match.group(1).strip()
            if provider.lower() == "private key":
                return "private key"
            return f"{provider} API key"
        return "API key"
    if span.cls == "secret" and span.subclass == "canary":
        return "canary token"
    base = base_label(span.cls, span.subclass)
    label = _NICE_LABELS.get(base, PLACEHOLDER_DESCRIPTIONS.get(base))
    if label is None:
        label = base.replace("_", " ").lower() or "redacted value"
    return label


def kinds_for_spans(spans: list[Any]) -> list[dict[str, Any]]:
    """Group spans into ``[{label, count}]`` (first-appearance order)."""
    kinds: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for span in sorted(spans, key=lambda s: (s.cp_start, s.cp_end)):
        label = span_label(span)
        entry = index.get(label)
        if entry is None:
            entry = {"label": label, "count": 0}
            index[label] = entry
            kinds.append(entry)
        entry["count"] += 1
    return kinds


def _why_for(
    text: str,
    gate_decision: Any,
    kinds: list[dict[str, Any]],
) -> str:
    """Plain-words reason for a BLOCK verdict (never values)."""
    from . import engine as engine_module

    reid_info = (gate_decision.evidence or {}).get("reid", {}) or {}
    if (
        isinstance(reid_info, dict)
        and reid_info.get("outcome") == "BLOCK"
        and reid_info.get("banner")
    ):
        return str(reid_info["banner"]) + " Nothing was uploaded."
    ordered = sorted(gate_decision.spans, key=lambda s: (s.cp_start, s.cp_end))
    if any(s.subclass == "canary" and s.status == "proven" for s in ordered):
        return (
            "A planted canary token was found (proven by code). "
            "Nothing was uploaded."
        )
    for span in ordered:
        if (
            span.subclass == "provider_key"
            and span.status == "proven"
            and span.message
        ):
            return span.message.replace("not sent", "not uploaded") + "."
    if kinds:
        parts = [_plural(item["count"], item["label"]) for item in kinds]
        return (
            "Found " + ", ".join(parts) + " in this file. "
            "Nothing was uploaded."
        )
    if isinstance(reid_info, dict) and reid_info.get("banner"):
        return str(reid_info["banner"]) + " Nothing was uploaded."
    cue = engine_module.cue_word(text)
    if cue is not None:
        return (
            f"This file mentions {cue!r} and needs a human review. "
            "Nothing was uploaded."
        )
    return "This file needs a human review. Nothing was uploaded."


def estimate_deep_check_seconds(chars: int) -> float:
    """Estimated deep-check time for ``chars`` characters.

    Formula: ``estimate = SETUP_S + n_chunks * SEC_PER_CHUNK`` with
    ``n_chunks = min(ceil(chars / CHUNK_CHARS), MAX_CHUNKS)`` (at least
    one chunk). Positive for every input and grows with file size.
    """
    size = max(0, int(chars))
    chunks = max(1, -(-size // DEEP_CHECK_CHUNK_CHARS))
    chunks = min(chunks, DEEP_CHECK_MAX_CHUNKS)
    return DEEP_CHECK_SETUP_S + chunks * DEEP_CHECK_SEC_PER_CHUNK


def deep_check_available(settings: Any = None) -> bool:
    """True when a Glimmer deep check could run (never probes Ollama).

    Kept probe-free so every scan stays fast: a scan that waited on a
    2 s Ollama timeout could never meet the ~1 s budget. Optimistic by
    design — a run-time failure surfaces as an ``unavailable`` job.
    """
    try:
        from . import hosted as hosted_module

        if hosted_module.is_hosted():
            return False
    except Exception:
        pass
    try:
        if settings is None:
            from .config import get_settings

            settings = get_settings()
        return str(getattr(settings, "tier2_mode", "live")).lower() != "off"
    except Exception:
        return True


def _deep_check_payload(chars: int, settings: Any = None) -> dict[str, Any]:
    return {
        "available": deep_check_available(settings),
        "estimate_seconds": estimate_deep_check_seconds(chars),
    }


def _record_payload(record: ScanRecord, settings: Any = None) -> dict[str, Any]:
    return {
        "decision": record.decision,
        "file_sha256": record.sha256,
        "kinds": [dict(item) for item in record.kinds],
        "why": record.why,
        "has_secret": record.has_secret,
        "identity_bundle": record.identity_bundle,
        "has_financial": record.has_financial,
        "overridable": record.overridable,
        "pages": record.pages,
        "chars": record.chars,
        "code_ms": round(record.code_ms, 3),
        "deep_check": _deep_check_payload(record.chars, settings),
        "scan_id": record.scan_id,
    }


def _store_unscannable(
    name: str, sha256: str, why: str, settings: Any = None
) -> dict[str, Any]:
    record = ScanRecord(
        scan_id=uuid.uuid4().hex,
        name=name,
        kind="unknown",
        text="",
        sha256=sha256,
        decision="UNSCANNABLE",
        why=why,
    )
    SCANS.save(record)
    log.info("fileguard unscannable: name=%s chars=0", _safe_name(name))
    return _record_payload(record, settings)


def _safe_name(name: str) -> str:
    """File name trimmed for logs (names, unlike contents, are fine)."""
    return (name or "upload")[:80]


def _extract_guarded(name: str, data: bytes) -> tuple[str, str, int]:
    """Extract ``(kind, text, pages)`` under the 25 MB guard cap.

    Raises :class:`UnsupportedType` for an unknown suffix and
    :class:`Unscannable` when a supported file yields no usable text.
    Encrypted PDFs are unscannable (never guessed at).
    """
    from .extract import (
        UnsupportedType,
        Unscannable,
        _decode_text,
        _extract_docx,
    )

    lowered = name.lower()
    if lowered == ".env" or lowered.endswith(".env"):
        text = _decode_text(data)
        if not text.strip():
            raise Unscannable("file has no readable text")
        return "text", text, 1
    suffix = "." + lowered.rpartition(".")[2] if "." in lowered else ""
    if suffix in TEXT_SUFFIXES:
        text = _decode_text(data)
        if not text.strip():
            raise Unscannable("file has no readable text")
        return "text", text, 1
    if suffix == ".docx":
        return "docx", _extract_docx(data), 1
    if suffix == ".pdf":
        return _extract_pdf_guarded(data)
    raise UnsupportedType(f"unsupported file type: {name!r}")


def _extract_pdf_guarded(data: bytes) -> tuple[str, str, int]:
    """Extract ``(kind, text, pages)`` from PDF bytes (encrypted → fail)."""
    from io import BytesIO

    from .extract import Unscannable

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is required
        raise Unscannable("pdf support unavailable") from exc
    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise Unscannable(f"cannot parse pdf: {type(exc).__name__}") from exc
    try:
        if getattr(reader, "is_encrypted", False):
            raise Unscannable("PDF is encrypted")
        pages = list(reader.pages)
    except Unscannable:
        raise
    except Exception as exc:
        raise Unscannable(f"cannot parse pdf: {type(exc).__name__}") from exc
    try:
        texts = [page.extract_text() or "" for page in pages]
    except Exception as exc:
        raise Unscannable(f"cannot parse pdf: {type(exc).__name__}") from exc
    text = "\n".join(part for part in texts if part.strip())
    if not text.strip():
        raise Unscannable("pdf has no readable text")
    return "pdf", text, len(pages)


def summarize_text(
    text: str,
    policy: Any,
    registry: Any,
) -> tuple[dict[str, Any], Any, float]:
    """Gate extracted text and summarise it for a file-guard card.

    Returns ``(fields, gate_decision, code_ms)`` where ``fields`` holds
    ``decision`` / ``kinds`` / ``why`` / ``has_secret`` /
    ``identity_bundle`` / ``has_financial`` / ``overridable``. Shared by
    the extension scan route and the console upload flow.
    """
    started = time.perf_counter()
    gate_decision = _code_gate(text, policy, registry)
    code_ms = (time.perf_counter() - started) * 1000
    verdict = gate_decision.decision
    if verdict == "ALLOW":
        fields: dict[str, Any] = {
            "decision": "CLEAN",
            "kinds": [],
            "why": "",
            "has_secret": False,
            "identity_bundle": False,
            "has_financial": False,
            "overridable": False,
        }
        return fields, gate_decision, code_ms
    kinds = kinds_for_spans(list(gate_decision.spans))
    reid_info = (gate_decision.evidence or {}).get("reid", {}) or {}
    identity_bundle = (
        isinstance(reid_info, dict) and reid_info.get("outcome") == "BLOCK"
    )
    if identity_bundle:
        kinds.append({"label": "full identity bundle", "count": 1})
    has_secret = any(span.cls == "secret" for span in gate_decision.spans)
    has_financial = any(
        span.cls == "pii" and span.subclass in _FINANCIAL_SUBCLASSES
        for span in gate_decision.spans
    )
    fields = {
        "decision": "BLOCK",
        "kinds": kinds,
        "why": _why_for(text, gate_decision, kinds),
        "has_secret": has_secret,
        "identity_bundle": identity_bundle,
        "has_financial": has_financial,
        # Secrets, canaries, identity bundles and financial identifiers
        # can never be overridden; lighter personal data only (names,
        # emails, phones) may offer "Upload anyway" behind a second
        # confirm, like the text WARN/REDACT rules.
        "overridable": (
            not has_secret and not identity_bundle and not has_financial
        ),
    }
    return fields, gate_decision, code_ms


def store_scan(
    name: str,
    kind: str,
    text: str,
    fields: dict[str, Any],
    code_ms: float,
    *,
    pages: int = 1,
    settings: Any = None,
) -> dict[str, Any]:
    """Store one already-gated text and return its card payload.

    The console upload flow gates its own way but reuses the same
    verdict store, so ``/api/scans/...`` redact and deep-check work
    for console uploads exactly like extension scans.
    """
    record = ScanRecord(
        scan_id=uuid.uuid4().hex,
        name=name,
        kind=kind,
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        decision=fields["decision"],
        why=fields.get("why", ""),
        kinds=list(fields.get("kinds", [])),
        has_secret=bool(fields.get("has_secret", False)),
        identity_bundle=bool(fields.get("identity_bundle", False)),
        has_financial=bool(fields.get("has_financial", False)),
        overridable=bool(fields.get("overridable", False)),
        pages=pages,
        chars=len(text),
        code_ms=code_ms,
    )
    SCANS.save(record)
    return _record_payload(record, settings)


def scan_upload(
    filename: str,
    data: bytes,
    policy: Any,
    registry: Any,
    *,
    settings: Any = None,
    glimmer_transport: Any = None,
    sam_transport: Any = None,
) -> dict[str, Any]:
    """Extract, gate and store one guarded file; return its card payload.

    Never raises for content problems: oversized, unsupported,
    encrypted or unparseable input becomes ``UNSCANNABLE`` so the file
    stays blocked. Audio has no local checker and is UNSCANNABLE too
    (transcribing it in the cloud would leak the file).
    """
    from . import image_redact as image_module
    from .extract import Unscannable, UnsupportedType, is_audio_file

    name = filename or "upload"
    sha256 = hashlib.sha256(bytes(data)).hexdigest()
    if len(data) > EXT_SCAN_MAX_BYTES:
        return _store_unscannable(
            name,
            sha256,
            "This file is larger than 25 MB, so it can't be checked. "
            "Nothing was uploaded.",
            settings,
        )
    if is_audio_file(name):
        return _store_unscannable(
            name,
            sha256,
            "Audio files can't be checked yet. Nothing was uploaded.",
            settings,
        )
    if image_module.is_image_file(name):
        return _scan_image(
            name,
            bytes(data),
            sha256,
            policy,
            registry,
            settings=settings,
            glimmer_transport=glimmer_transport,
            sam_transport=sam_transport,
        )
    try:
        kind, text, pages = _extract_guarded(name, bytes(data))
    except UnsupportedType:
        return _store_unscannable(
            name,
            sha256,
            "This file type can't be checked. Nothing was uploaded.",
            settings,
        )
    except Unscannable as exc:
        return _store_unscannable(
            name,
            sha256,
            f"Couldn't check this file: {exc}. Nothing was uploaded.",
            settings,
        )
    fields, _gate_decision, code_ms = summarize_text(text, policy, registry)
    record = ScanRecord(
        scan_id=uuid.uuid4().hex,
        name=name,
        kind=kind,
        text=text,
        sha256=sha256,
        decision=fields["decision"],
        why=fields["why"],
        kinds=fields["kinds"],
        has_secret=fields["has_secret"],
        identity_bundle=fields["identity_bundle"],
        has_financial=fields["has_financial"],
        overridable=fields["overridable"],
        pages=pages,
        chars=len(text),
        code_ms=code_ms,
    )
    SCANS.save(record)
    log.info(
        "fileguard scan: decision=%s kinds=%d chars=%d code_ms=%.1f",
        record.decision,
        len(record.kinds),
        record.chars,
        record.code_ms,
    )
    return _record_payload(record, settings)


def _scan_image(
    name: str,
    data: bytes,
    sha256: str,
    policy: Any,
    registry: Any,
    *,
    settings: Any = None,
    glimmer_transport: Any = None,
    sam_transport: Any = None,
) -> dict[str, Any]:
    """Scan one image with the local box-fill flow (no cloud SAM).

    The extension guard never sends the original anywhere: masks come
    from local boxes only. When box-fill is unavailable the image is
    UNSCANNABLE and stays blocked.
    """
    from . import image_redact as image_module

    try:
        redacted = image_module.redact_image(
            data,
            settings=settings,
            glimmer_transport=glimmer_transport,
            sam_transport=sam_transport,
            sam_enabled=False,
            policy=policy,
            registry=registry,
        )
    except image_module.ImageRedactError as exc:
        return _store_unscannable(
            name,
            sha256,
            f"Couldn't check this image: {exc}. Nothing was uploaded.",
            settings,
        )
    text = "\n".join(redacted.texts)
    regions = len(redacted.regions)
    if text.strip():
        fields, _gate_decision, code_ms = summarize_text(
            text, policy, registry
        )
    else:
        fields = {
            "decision": "CLEAN",
            "kinds": [],
            "why": "",
            "has_secret": False,
            "identity_bundle": False,
            "has_financial": False,
            "overridable": False,
        }
        code_ms = 0.0
    kinds = list(fields["kinds"])
    if regions:
        kinds.append({"label": "sensitive image region", "count": regions})
    if regions and fields["decision"] == "CLEAN":
        fields = {
            "decision": "BLOCK",
            "kinds": kinds,
            "why": (
                f"Found {_plural(regions, 'sensitive image region')} "
                "in this image. Nothing was uploaded."
            ),
            "has_secret": fields["has_secret"],
            "identity_bundle": False,
            "has_financial": fields.get("has_financial", False),
            "overridable": (
                not fields["has_secret"]
                and not fields.get("has_financial", False)
            ),
        }
    else:
        fields = dict(fields, kinds=kinds)
    record = ScanRecord(
        scan_id=uuid.uuid4().hex,
        name=name,
        kind="image",
        text=text,
        sha256=sha256,
        decision=fields["decision"],
        why=fields["why"],
        kinds=fields["kinds"],
        has_secret=fields["has_secret"],
        identity_bundle=fields["identity_bundle"],
        has_financial=fields.get("has_financial", False),
        overridable=fields["overridable"],
        pages=1,
        chars=len(text),
        code_ms=code_ms,
        image_png=redacted.png,
    )
    SCANS.save(record)
    log.info(
        "fileguard scan image: decision=%s regions=%d chars=%d",
        record.decision,
        regions,
        record.chars,
    )
    return _record_payload(record, settings)


def _scrub_reid_ranges(text: str) -> str:
    """Replace every re-id finding range with the scrub token."""
    from . import reid as reid_module

    try:
        findings = reid_module.extract_quasi_identifiers(text)
    except Exception:
        return text
    ranges = sorted(
        {
            (int(item["span"][0]), int(item["span"][1]))
            for item in findings
            if isinstance(item.get("span"), (list, tuple))
            and len(item["span"]) == 2
        }
    )
    merged: list[list[int]] = []
    for start, end in ranges:
        if start < 0 or end > len(text) or start >= end:
            continue
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    if not merged:
        return text
    out = text
    for start, end in reversed(merged):
        out = out[:start] + SCRUB_TOKEN + out[end:]
    return out


def _scrub_cues(text: str) -> str:
    """Replace sensitivity-cue mentions with the scrub token."""
    from . import engine as engine_module

    out = text
    for _ in range(10):
        cue = engine_module.cue_word(out)
        if cue is None:
            break
        if cue == "unc path":
            out = re.sub(r"\\\\[A-Za-z0-9_.$-]+", SCRUB_TOKEN, out)
        else:
            pattern = re.compile(
                r"\s+".join(re.escape(word) for word in cue.split(" ")),
                re.IGNORECASE,
            )
            out = pattern.sub(SCRUB_TOKEN, out)
    return out


def sanitize_text(text: str, policy: Any, registry: Any) -> str:
    """Scrub text until the code gate says ALLOW (at most N passes).

    Each pass redacts every proven and ambiguous span with
    placeholders, then scrubs re-identification ranges and cue-word
    mentions. Raises :class:`FileguardError` when the text still flags
    afterwards, so a risky copy is never released.
    """
    candidate = text
    for _ in range(MAX_SANITIZE_PASSES):
        gate_decision = _code_gate(candidate, policy, registry)
        if gate_decision.decision == "ALLOW":
            return candidate
        spans = [
            span
            for span in gate_decision.spans
            if span.status in ("proven", "ambiguous")
        ]
        if spans:
            candidate, _mapping = redact(candidate, spans)
            continue
        scrubbed = _scrub_reid_ranges(candidate)
        if scrubbed != candidate:
            candidate = scrubbed
            continue
        cue_scrubbed = _scrub_cues(candidate)
        if cue_scrubbed != candidate:
            candidate = cue_scrubbed
            continue
        break
    gate_decision = _code_gate(candidate, policy, registry)
    if gate_decision.decision != "ALLOW":
        raise FileguardError(
            "The redacted copy still flags as sensitive, so it was "
            "withheld. Nothing was uploaded.",
            decision="BLOCK",
        )
    return candidate


def _redacted_name(name: str, kind: str) -> tuple[str, str]:
    """Return ``(filename, note)`` for a redacted copy of ``name``."""
    base = (name or "upload").rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or "upload"
    if kind in ("docx", "pdf"):
        stem = base.rpartition(".")[0] or base
        return f"{stem}.redacted.txt", LAYOUT_NOTE
    if "." in base and not base.startswith("."):
        stem, _dot, suffix = base.rpartition(".")
        return f"{stem}.redacted.{suffix}", ""
    return f"{base}.redacted.txt", ""


def build_redacted_copy(scan_id: str) -> dict[str, Any]:
    """Build the downloadable redacted copy for one BLOCK scan.

    Text-likes keep their format with placeholders / scrub tokens;
    DOCX and PDF become ``<stem>.redacted.txt``; images return the
    stored box-filled PNG. The copy always scans CLEAN. Raises
    :class:`UnknownScan` for a missing id and :class:`RedactRefused`
    when there is nothing to redact.
    """
    from .app import get_policy, get_registry

    record = SCANS.load((scan_id or "").strip())
    if record is None:
        raise UnknownScan()
    if record.decision == "CLEAN":
        raise RedactRefused(
            "This file is already clean — there is nothing to redact.",
            decision="CLEAN",
        )
    if record.decision != "BLOCK":
        raise RedactRefused(record.why or "This file can't be redacted.")
    if record.kind == "image":
        if not record.image_png:
            raise RedactRefused(
                "No redacted image is available for this file."
            )
        filename, _note = _redacted_name(record.name, "image")
        if not filename.lower().endswith(".png"):
            filename += ".png"
        return {
            "filename": filename,
            "media_type": "image/png",
            "content_base64": base64.b64encode(record.image_png).decode("ascii"),
            "note": "",
            "chars": record.chars,
            "clean": True,
        }
    policy, registry = get_policy(), get_registry()
    clean_text = sanitize_text(record.text, policy, registry)
    filename, note = _redacted_name(record.name, record.kind)
    lowered = record.name.lower()
    suffix = "." + lowered.rpartition(".")[2] if "." in lowered else ""
    media_type = _MEDIA_TYPES.get(suffix, "text/plain")
    raw = clean_text.encode("utf-8")
    return {
        "filename": filename,
        "media_type": media_type,
        "content_base64": base64.b64encode(raw).decode("ascii"),
        "note": note,
        "chars": len(clean_text),
        "clean": True,
    }


# --- deep check -------------------------------------------------------------


@dataclass
class DeepCheckJob:
    """One Glimmer deep-check run (memory only, 15 minute TTL)."""

    check_id: str
    scan_id: str
    status: str  # running | done | unavailable | failed
    progress: float = 0.0
    findings: list[dict[str, Any]] = field(default_factory=list)
    chars_checked: int = 0
    truncated: bool = False
    error: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + SCAN_TTL_S)


_JOBS: dict[str, DeepCheckJob] = {}
_JOBS_LOCK = threading.Lock()


def _job_payload(job: DeepCheckJob) -> dict[str, Any]:
    return {
        "check_id": job.check_id,
        "scan_id": job.scan_id,
        "status": job.status,
        "progress": round(job.progress, 3),
        "findings": [dict(item) for item in job.findings],
        "chars_checked": job.chars_checked,
        "truncated": job.truncated,
        "error": job.error,
    }


def get_deep_check(check_id: str) -> dict[str, Any] | None:
    """Return one deep-check job payload, or None when unknown/expired."""
    with _JOBS_LOCK:
        job = _JOBS.get(check_id or "")
        if job is None:
            return None
        if time.time() > job.expires_at:
            del _JOBS[check_id]
            return None
        return _job_payload(job)


def merge_deep_findings(
    code_kinds: list[dict[str, Any]],
    deep_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge deep-check findings over code kinds (only ever adds).

    Code kinds keep their labels and counts untouched; deep-check
    labels the code pass missed are appended. A deep check that finds
    nothing leaves the code verdict exactly as it was: it can never
    clear a code finding.
    """
    merged = [dict(item) for item in code_kinds]
    seen = {str(item.get("label", "")).lower() for item in merged}
    for finding in deep_findings:
        label = str(finding.get("label", "")).strip()
        if not label or label.lower() in seen:
            continue
        try:
            count = max(1, int(finding.get("count", 1)))
        except (TypeError, ValueError):
            count = 1
        merged.append({"label": label, "count": count})
        seen.add(label.lower())
    return merged


def chunk_text(text: str) -> list[str]:
    """Split text into deep-check chunks (capped, never empty)."""
    size = max(0, len(text or ""))
    chunks = [
        text[start : start + DEEP_CHECK_CHUNK_CHARS]
        for start in range(0, size, DEEP_CHECK_CHUNK_CHARS)
    ]
    return chunks[:DEEP_CHECK_MAX_CHUNKS] or [""]


_DEEP_CHECK_SYSTEM = (
    "You check text a user is about to attach to an outside AI for "
    "sensitive details the code pass may have missed: personal data, "
    "contact details, account numbers, passwords and secrets. Reply "
    "with JSON only, exactly: {\"findings\": [{\"label\": \"<one of: "
    "email address, phone number, person name, street address, date "
    "of birth, account number, secret, password, API key, canary "
    "token>\", \"quote\": \"<exact quote copied from the text>\"}]}. "
    "One entry per finding; empty list when nothing is sensitive."
)


def glimmer_chunk_judge(
    chunk: str,
    *,
    settings: Any = None,
    transport: Any = None,
) -> list[dict[str, str]]:
    """Ask local Glimmer for one chunk's extra findings.

    Returns ``[{label, quote}]`` with labels from the fixed vocabulary
    and quotes verified as exact substrings. Raises
    :class:`FileguardError` on any failure so the job reports
    ``unavailable`` instead of guessing.
    """
    import httpx

    from .config import get_settings
    from .glimmer import (
        glimmer_keep_alive,
        glimmer_options,
        glimmer_timeout_s,
        parse_glimmer_json,
    )
    from .upstream import _verify_for

    cfg = settings or get_settings()
    body = {
        "model": cfg.judge_model,
        "messages": [
            {"role": "system", "content": _DEEP_CHECK_SYSTEM},
            {"role": "user", "content": chunk},
        ],
        "format": "json",
        "stream": False,
        "think": False,
        "keep_alive": glimmer_keep_alive(),
        "options": glimmer_options(cfg),
    }
    url = cfg.ollama_url.rstrip("/") + "/api/chat"
    try:
        with httpx.Client(
            transport=transport,
            timeout=glimmer_timeout_s(cfg),
            verify=_verify_for(url),
        ) as client:
            response = client.post(url, json=body)
        payload = response.json()
    except Exception as exc:
        raise FileguardError(
            "Muse Glimmer isn't answering, so the deep check can't run.",
            decision="UNSCANNABLE",
        ) from exc
    if response.status_code != 200:
        raise FileguardError(
            "Muse Glimmer isn't answering, so the deep check can't run.",
            decision="UNSCANNABLE",
        )
    message = payload.get("message", {}) if isinstance(payload, dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    try:
        parsed = parse_glimmer_json(content)
    except ValueError as exc:
        raise FileguardError(
            "Muse Glimmer gave an unreadable answer, so the deep check "
            "can't complete.",
            decision="UNSCANNABLE",
        ) from exc
    items = parsed.get("findings", []) if isinstance(parsed, dict) else []
    if not isinstance(items, list):
        raise FileguardError(
            "Muse Glimmer gave an unreadable answer, so the deep check "
            "can't complete.",
            decision="UNSCANNABLE",
        )
    findings: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "") or "").strip().lower()
        quote = str(item.get("quote", "") or "")
        if not quote or quote not in chunk:
            continue
        if label not in DEEP_CHECK_LABELS:
            label = "sensitive detail"
        findings.append({"label": label, "quote": quote})
    return findings


def _aggregate(findings: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Count verified chunk findings by label (labels only, no quotes)."""
    counts: dict[str, int] = {}
    for item in findings:
        label = item["label"]
        counts[label] = counts.get(label, 0) + 1
    return [
        {"label": label, "count": counts[label]} for label in counts
    ]


def _run_deep_check(
    job: DeepCheckJob,
    text: str,
    judge_fn: Any,
    transport: Any,
    settings: Any,
) -> None:
    """Background worker: judge every chunk, then publish findings."""
    chunks = chunk_text(text)
    total = len(chunks)
    verified: list[dict[str, str]] = []
    try:
        for index, chunk in enumerate(chunks):
            if judge_fn is not None:
                raw = judge_fn(chunk)
                items = raw if isinstance(raw, list) else []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    label = str(item.get("label", "") or "").strip().lower()
                    quote = str(item.get("quote", "") or "")
                    if not quote or quote not in chunk:
                        continue
                    if label not in DEEP_CHECK_LABELS:
                        label = "sensitive detail"
                    verified.append({"label": label, "quote": quote})
            else:
                verified.extend(
                    glimmer_chunk_judge(
                        chunk, settings=settings, transport=transport
                    )
                )
            with _JOBS_LOCK:
                job.progress = (index + 1) / total
                job.chars_checked += len(chunk)
    except FileguardError as exc:
        with _JOBS_LOCK:
            job.status = "unavailable"
            job.error = str(exc)
        return
    except Exception:
        with _JOBS_LOCK:
            job.status = "failed"
            job.error = "The deep check hit an unexpected error."
        return
    with _JOBS_LOCK:
        job.status = "done"
        job.progress = 1.0
        job.findings = _aggregate(verified)
        job.truncated = len(text or "") > sum(len(c) for c in chunks)


def start_deep_check(
    scan_id: str,
    *,
    judge_fn: Any = None,
    transport: Any = None,
    settings: Any = None,
) -> dict[str, Any]:
    """Start a Glimmer deep check over one scan's text (background).

    Returns the job payload; poll with :func:`get_deep_check`. An
    injected ``judge_fn`` (tests) always runs; the live Glimmer path
    reports ``unavailable`` immediately when Tier 2 is off or the app
    runs hosted. Raises :class:`UnknownScan` for a missing id and
    :class:`RedactRefused`-style :class:`FileguardError` when the scan
    holds no text to check.
    """
    record = SCANS.load((scan_id or "").strip())
    if record is None:
        raise UnknownScan()
    if not record.text.strip():
        raise FileguardError(
            "There is no readable text to deep-check.",
            decision=record.decision,
        )
    job = DeepCheckJob(
        check_id=uuid.uuid4().hex,
        scan_id=record.scan_id,
        status="running",
    )
    if judge_fn is None and not deep_check_available(settings):
        job.status = "unavailable"
        try:
            from . import hosted as hosted_module

            hosted = hosted_module.is_hosted()
        except Exception:
            hosted = False
        job.error = (
            "Deep check isn't available in the public demo."
            if hosted
            else "Deep check is off (Tier 2 is disabled)."
        )
        with _JOBS_LOCK:
            _JOBS[job.check_id] = job
        return _job_payload(job)
    with _JOBS_LOCK:
        _JOBS[job.check_id] = job
    worker = threading.Thread(
        target=_run_deep_check,
        args=(job, record.text, judge_fn, transport, settings),
        name="sf-deep-check",
        daemon=True,
    )
    worker.start()
    return _job_payload(job)
