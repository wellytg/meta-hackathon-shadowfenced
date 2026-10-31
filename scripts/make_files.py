"""Generate synthetic demo upload files under runs/files/ (T12).

Every value is fictional: invented names, ``@corvane.io`` emails (never
``@example.*``), and ``555-01XX`` phone numbers. The memo PDF carries one
canary line generated at runtime from the configured seed, so no canary
value is ever committed. Run: ``python scripts/make_files.py``.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shadowfenced.canary import CanaryRegistry  # noqa: E402
from shadowfenced.config import get_settings  # noqa: E402

OUT_DIR = ROOT / "runs" / "files"

PEOPLE = [
    ("Ada Lark", "ada.lark@corvane.io", "+1 (415) 555-0132"),
    ("Ben Okafor", "ben.okafor@corvane.io", "+1 (415) 555-0147"),
    ("Cara Mendez", "cara.mendez@corvane.io", "+1 (415) 555-0158"),
    ("Dev Patel", "dev.patel@corvane.io", "+1 (415) 555-0161"),
    ("Elin Ross", "elin.ross@corvane.io", "+1 (415) 555-0174"),
    ("Finn Gallagher", "finn.gallagher@corvane.io", "+1 (415) 555-0189"),
    ("Greta Hoff", "greta.hoff@corvane.io", "+1 (415) 555-0193"),
    ("Hugo Reyes", "hugo.reyes@corvane.io", "+1 (415) 555-0116"),
]


def write_customers_csv(path: Path) -> None:
    """8 fictional customer rows: name, email, phone."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "email", "phone"])
        writer.writerows(PEOPLE)


def write_roadmap_docx(path: Path) -> None:
    """Fictional roadmap mentioning Project TALLOW and Aster Freight."""
    from docx import Document

    document = Document()
    document.add_heading("Corvane Labs roadmap (fictional demo)", level=1)
    document.add_paragraph(
        "Project TALLOW ships in two phases. Phase one covers the "
        "internal pilot; phase two opens the shared workspace."
    )
    document.add_paragraph(
        "Our client Aster Freight asked for a status summary before "
        "the pilot review. Nothing in this note is sensitive."
    )
    table = document.add_table(rows=3, cols=2)
    table.rows[0].cells[0].text = "Milestone"
    table.rows[0].cells[1].text = "Owner"
    table.rows[1].cells[0].text = "Pilot review"
    table.rows[1].cells[1].text = "Ada Lark"
    table.rows[2].cells[0].text = "Workspace launch"
    table.rows[2].cells[1].text = "Ben Okafor"
    document.save(str(path))


def write_notes_md(path: Path) -> None:
    """Benign meeting notes with nothing to flag."""
    path.write_text(
        "# Standup notes (fictional demo)\n\n"
        "- Water the office plants on Fridays.\n"
        "- The build takes about four minutes on a cold cache.\n"
        "- Bring lunch ideas to the Thursday thread.\n",
        encoding="utf-8",
    )


def _minimal_pdf_bytes(lines: list[str]) -> bytes:
    """Build a minimal one-page PDF whose text extracts via pypdf."""
    safe = [
        line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        for line in lines
    ]
    content = "BT /F1 12 Tf 72 720 Td 14 TL " + " T* ".join(
        f"({part}) Tj" for part in safe
    ) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content.encode('latin-1'))} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


def write_memo_pdf(path: Path) -> None:
    """One-page memo with a single runtime canary line (never committed)."""
    seed = get_settings().canary_seed
    canary = CanaryRegistry(seed, ("db_password",)).value("db_password")
    path.write_bytes(
        _minimal_pdf_bytes(
            [
                "Corvane Labs internal memo (fictional demo)",
                f"debug password: {canary}",
            ]
        )
    )


def main(out_dir: Path = OUT_DIR) -> Path:
    """Write all four demo files and return the directory."""
    out_dir.mkdir(parents=True, exist_ok=True)
    write_customers_csv(out_dir / "customers.csv")
    write_roadmap_docx(out_dir / "roadmap.docx")
    write_notes_md(out_dir / "notes.md")
    write_memo_pdf(out_dir / "memo.pdf")
    return out_dir


if __name__ == "__main__":
    target = main()
    print(f"wrote demo files to {target}")
