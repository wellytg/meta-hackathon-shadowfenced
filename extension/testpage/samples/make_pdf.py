"""Build a PDF version of a text sample at test time (stdlib only).

Usage:  python make_pdf.py <input.txt> <output.pdf>

Writes a minimal multi-page PDF (Helvetica, 60 lines per page) so tests
can exercise the PDF path without committing binaries. Non-Latin-1
characters are replaced with ``?``.
"""

from __future__ import annotations

import sys
from pathlib import Path

LINES_PER_PAGE = 60


def _escape(line: str) -> str:
    """Escape one line for a PDF literal string (Latin-1 only)."""
    out = line.encode("latin-1", errors="replace").decode("latin-1")
    return out.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_to_pdf_bytes(text: str) -> bytes:
    """Render plain text as a minimal PDF (one Helvetica font)."""
    lines = (text or "").splitlines() or [""]
    pages = [lines[i : i + LINES_PER_PAGE] for i in range(0, len(lines), LINES_PER_PAGE)]
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")  # 1
    objects.append(  # 2
        ("<< /Type /Pages /Kids ["
         + " ".join(f"{3 + i} 0 R" for i in range(len(pages)))
         + f"] /Count {len(pages)} >>").encode("ascii")
    )
    first_stream = 3 + len(pages)
    for index, chunk in enumerate(pages):
        content = ["BT /F1 10 Tf 50 780 Td 12 TL"]
        for line in chunk:
            content.append(f"({_escape(line)}) Tj T*")
        content.append("ET")
        stream = "\n".join(content).encode("latin-1")
        objects.append(  # page object
            (f"<< /Type /Page /Parent 2 0 R "
             f"/MediaBox [0 0 612 792] "
             f"/Resources << /Font << /F1 {first_stream + len(pages)} 0 R >> >> "
             f"/Contents {first_stream + index} 0 R >>").encode("ascii")
        )
    for chunk in pages:  # content streams (order matches the loop above)
        content = ["BT /F1 10 Tf 50 780 Td 12 TL"]
        for line in chunk:
            content.append(f"({_escape(line)}) Tj T*")
        content.append("ET")
        stream = "\n".join(content).encode("latin-1")
        objects.append(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
            + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    pdf += b"0000000000 65535 f \n"
    for offset in offsets:
        pdf += f"{offset:010d} 00000 n \n".encode("ascii")
    pdf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(pdf)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python make_pdf.py <input.txt> <output.pdf>", file=sys.stderr)
        return 2
    src, dst = Path(argv[1]), Path(argv[2])
    dst.write_bytes(text_to_pdf_bytes(src.read_text(encoding="utf-8")))
    print(f"wrote {dst} ({dst.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
