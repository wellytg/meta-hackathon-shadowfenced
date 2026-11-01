"""In-memory upload store (T12): extracted text by ``file_id``, 15 min TTL.

The text is kept only in memory so the gate can scan it at send time;
it is never written to disk or the decision log.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

STORE_TTL_S = 15 * 60


@dataclass
class UploadedFile:
    """One uploaded file's extracted text."""

    file_id: str
    name: str
    kind: str
    text: str
    status: str  # clean | flagged | unscannable
    image_png: bytes | None = None  # redacted PNG bytes (images only)
    regions: int = 0  # sensitive regions hidden (images only)
    thumbnail: str = ""  # redacted thumbnail data URL (images only)
    method: str = ""  # "sam" | "box" | "none" (images only)
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + STORE_TTL_S)


class UploadStore:
    """Thread-safe in-memory upload registry."""

    def __init__(self) -> None:
        self._files: dict[str, UploadedFile] = {}
        self._lock = threading.Lock()

    def save(self, name: str, kind: str, text: str, status: str) -> UploadedFile:
        record = UploadedFile(
            file_id=uuid.uuid4().hex, name=name, kind=kind, text=text,
            status=status,
        )
        with self._lock:
            self._files[record.file_id] = record
        return record

    def load(self, file_id: str) -> UploadedFile | None:
        """Return one record, or None when unknown or expired."""
        with self._lock:
            record = self._files.get(file_id)
            if record is None:
                return None
            if time.time() > record.expires_at:
                del self._files[file_id]
                return None
            return record

    def clear(self) -> None:
        with self._lock:
            self._files.clear()


UPLOADS = UploadStore()


def reset_for_tests() -> None:
    """Drop every upload (tests only)."""
    UPLOADS.clear()
