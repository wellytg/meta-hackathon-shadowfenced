"""Upload extraction (T12): bytes in, gated text out.

Supported suffixes map to a ``kind`` (the suffix without the dot):
``txt``, ``md``, ``csv`` decode as UTF-8; ``docx`` reads paragraphs and
table cells via python-docx; ``pdf`` reads page text via pypdf. Audio
(``wav``, ``mp3``, ``m4a``, ``ogg``, ``webm``; 10 MB max) is transcribed
by Meta's Muse Voice Transcribe model (see :mod:`shadowfenced.transcribe`)
and the transcript is scanned like any other document. Anything
else is unsupported (the route answers 415); oversized or unparseable
input is not an error — it extracts to ``unscannable`` so the gate can
HOLD the send instead of guessing.

Extracted text lives in :mod:`uploads` memory keyed by ``file_id`` with
a 15 minute TTL; it is never written to disk or the log.
"""

from __future__ import annotations

from io import BytesIO

MAX_FILE_BYTES = 2 * 1024 * 1024

# suffix -> kind
SUPPORTED: dict[str, str] = {
    ".txt": "text",
    ".md": "markdown",
    ".csv": "csv",
    ".docx": "docx",
    ".pdf": "pdf",
}


MAX_AUDIO_BYTES = 10 * 1024 * 1024

AUDIO_SUFFIXES: tuple[str, ...] = (".wav", ".mp3", ".m4a", ".ogg", ".webm")


def is_audio_file(name: str) -> bool:
    """True when the file name has a supported audio suffix."""
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in AUDIO_SUFFIXES)


class UnsupportedType(ValueError):
    """The file suffix is not one we extract."""


class FileTooLarge(ValueError):
    """The file exceeds the 2 MB upload cap."""


class Unscannable(ValueError):
    """The file claims a supported suffix but yields no usable text."""


def kind_for(name: str) -> str:
    """Return the extraction kind for a file name, or raise UnsupportedType."""
    lower = name.lower()
    for suffix, kind in SUPPORTED.items():
        if lower.endswith(suffix):
            return kind
    raise UnsupportedType(f"unsupported file type: {name!r}")


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Unscannable("file is not valid UTF-8") from exc


def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - dependency is required
        raise Unscannable("docx support unavailable") from exc
    try:
        document = Document(BytesIO(data))
    except Exception as exc:
        raise Unscannable(f"cannot parse docx: {type(exc).__name__}") from exc
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    parts.append(cell.text)
    text = "\n".join(parts)
    if not text.strip():
        raise Unscannable("docx has no readable text")
    return text


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is required
        raise Unscannable("pdf support unavailable") from exc
    try:
        reader = PdfReader(BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise Unscannable(f"cannot parse pdf: {type(exc).__name__}") from exc
    text = "\n".join(part for part in pages if part.strip())
    if not text.strip():
        raise Unscannable("pdf has no readable text")
    return text


def extract_text(name: str, data: bytes) -> tuple[str, str]:
    """Extract gateable text from one upload.

    Returns ``(kind, text)``. Raises :class:`UnsupportedType` for an
    unknown suffix, :class:`FileTooLarge` past 2 MB, and
    :class:`Unscannable` when a supported file yields no usable text.
    """
    kind = kind_for(name)
    if len(data) > MAX_FILE_BYTES:
        raise FileTooLarge(f"file exceeds {MAX_FILE_BYTES} bytes")
    if kind in ("text", "markdown", "csv"):
        text = _decode_text(data)
        if not text.strip():
            raise Unscannable("file has no readable text")
        return kind, text
    if kind == "docx":
        return kind, _extract_docx(data)
    if kind == "pdf":
        return kind, _extract_pdf(data)
    raise UnsupportedType(f"unsupported file type: {name!r}")  # pragma: no cover
