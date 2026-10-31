"""Extraction tests (T12): txt/md/csv/docx/pdf, unsupported, too large.

Docx and PDF fixtures are generated at test time in tmp_path; no binary
files are committed.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from shadowfenced import uploads
from shadowfenced.app import handle_upload
from shadowfenced.extract import (
    MAX_FILE_BYTES,
    FileTooLarge,
    Unscannable,
    UnsupportedType,
    extract_text,
    kind_for,
)


@pytest.fixture(autouse=True)
def _clean_uploads():
    uploads.reset_for_tests()
    yield
    uploads.reset_for_tests()


def _docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document

    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "Milestone"
    table.rows[0].cells[1].text = "Owner"
    table.rows[1].cells[0].text = "Pilot review"
    table.rows[1].cells[1].text = "Ada Lark"
    import io

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pdf_bytes(lines: list[str]) -> bytes:
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
        f"<< /Length {len(content.encode('latin-1'))} >>\n"
        f"stream\n{content}\nendstream",
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


def test_txt_extracts():
    kind, text = extract_text("note.txt", b"hello there")
    assert kind == "text"
    assert text == "hello there"


def test_md_extracts():
    kind, text = extract_text("NOTE.MD", b"# hello\nbody")
    assert kind == "markdown"
    assert "hello" in text


def test_csv_extracts_rows_verbatim():
    raw = b"name,email\nAda,ada@corvanelabs.com\n"
    kind, text = extract_text("customers.csv", raw)
    assert kind == "csv"
    assert text == raw.decode("utf-8")


def test_docx_extracts_paragraphs_and_table_cells():
    data = _docx_bytes(["Project TALLOW pilot", "client Aster Freight"])
    kind, text = extract_text("roadmap.docx", data)
    assert kind == "docx"
    assert "TALLOW" in text
    assert "Aster Freight" in text
    assert "Pilot review" in text  # table cell text is included


def test_pdf_extracts_page_text():
    data = _pdf_bytes(["hello pdf", "second line"])
    kind, text = extract_text("memo.pdf", data)
    assert kind == "pdf"
    assert "hello pdf" in text


def test_kind_for_is_case_insensitive():
    assert kind_for("A.DOCX") == "docx"
    assert kind_for("a.Pdf") == "pdf"


def test_unsupported_suffix_raises():
    with pytest.raises(UnsupportedType):
        extract_text("archive.tar.gz", b"data")
    with pytest.raises(UnsupportedType):
        kind_for("photo.png")


def test_too_large_raises():
    with pytest.raises(FileTooLarge):
        extract_text("big.txt", b"x" * (MAX_FILE_BYTES + 1))


def test_at_cap_is_accepted():
    kind, text = extract_text("big.txt", b"x" * MAX_FILE_BYTES)
    assert kind == "text"
    assert len(text) == MAX_FILE_BYTES


def test_empty_txt_is_unscannable():
    with pytest.raises(Unscannable):
        extract_text("empty.txt", b"   ")


def test_bad_utf8_is_unscannable():
    with pytest.raises(Unscannable):
        extract_text("bad.txt", b"\xff\xfe\x00bad")


def test_garbage_docx_is_unscannable():
    with pytest.raises(Unscannable):
        extract_text("broken.docx", b"not a zip file")


def test_garbage_pdf_is_unscannable():
    with pytest.raises(Unscannable):
        extract_text("broken.pdf", b"%PDF-not-really")


def test_handle_upload_unsupported_is_415():
    with pytest.raises(HTTPException) as exc_info:
        handle_upload("photo.png", b"data")
    assert exc_info.value.status_code == 415
    assert (
        exc_info.value.detail["code"] == "shadowfenced_unsupported_file"
    )


def test_handle_upload_too_large_is_unscannable_record():
    body = handle_upload("big.txt", b"x" * (MAX_FILE_BYTES + 1))
    assert body["status"] == "unscannable"
    assert body["chars"] == 0
    assert body["file_id"]
    assert uploads.UPLOADS.load(body["file_id"]) is not None


def test_handle_upload_clean_and_flagged_statuses():
    clean = handle_upload("notes.md", b"water the plants on fridays")
    assert clean["status"] == "clean"
    assert clean["chars"] > 0
    flagged = handle_upload(
        "leak.txt", b"mail ada@corvanelabs.com now"
    )
    assert flagged["status"] == "flagged"
    assert flagged["summary"].get("pii", 0) >= 1
