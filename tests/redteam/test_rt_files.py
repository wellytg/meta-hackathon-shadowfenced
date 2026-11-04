"""Red-team: secrets hiding in files (T79).

The file guard scans extracted text. It never looks at the file name or at
document metadata, so a key in a name (or a mailbox in a docx author field)
scans CLEAN. Two mitigations hold today: files with no readable text stay
UNSCANNABLE (never passed), and ``/api/send`` re-gates the file name when a
file is attached by id.
"""

from __future__ import annotations

from io import BytesIO

import pytest

from shadowfenced import fileguard, uploads
from shadowfenced.app import SendRequest, handle_upload, send
from helpers_rt import aws_key, email, make_policy, make_registry, upstream_mock


@pytest.fixture()
def policy():
    return make_policy()


@pytest.fixture()
def registry():
    return make_registry()


@pytest.fixture()
def mock_upstream():
    yield from upstream_mock()


@pytest.fixture(autouse=True)
def _clean_uploads():
    uploads.reset_for_tests()
    yield
    uploads.reset_for_tests()


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: file guard never scans the file name",
)
def test_key_in_file_name_scans_clean(policy, registry):
    """A key sitting in the file name must not scan CLEAN."""
    result = fileguard.scan_upload(
        f"deploy_{aws_key()}.txt",
        b"meeting notes, nothing sensitive in the body",
        policy, registry,
    )
    assert result["decision"] != "CLEAN"


def test_key_in_file_name_stopped_at_send_time(policy, registry, mock_upstream):
    """STOPPED (send path): attaching that file still BLOCKs the send."""
    calls = mock_upstream()
    uploaded = handle_upload(
        f"deploy_{aws_key()}.txt",
        b"meeting notes, nothing sensitive in the body",
    )
    body = send(SendRequest(
        messages=[{"role": "user", "content": "see attached notes"}],
        file_ids=[uploaded["file_id"]],
    ))
    assert body["decision"] == "BLOCK"
    assert body["sent_text"] is None
    assert calls == []


def test_csv_values_under_sensitive_headers_redacted(policy, registry):
    """STOPPED: CSV cells under name/email/ssn headers never go raw."""
    from shadowfenced import engine

    text = "name,email,ssn\nJon Smith,jon.smith@mail.example,123-45-6789\n"
    decision = engine.gate(text, policy, registry, mode="send")
    assert decision.decision in ("REDACT", "WARN", "BLOCK", "HOLD")
    if decision.sent_text is not None:
        assert "jon.smith@mail.example" not in decision.sent_text
        assert "123-45-6789" not in decision.sent_text


def test_json_email_value_redacted(policy, registry):
    """STOPPED: an email under a JSON key is redacted, never sent raw."""
    from shadowfenced import engine

    decision = engine.gate('{"email": "jon.smith@mail.example"}', policy,
                           registry, mode="send")
    assert decision.decision in ("REDACT", "WARN", "BLOCK", "HOLD")
    if decision.sent_text is not None:
        assert "jon.smith@mail.example" not in decision.sent_text


def test_json_password_value_held(policy, registry):
    """STOPPED: a password under a JSON key HOLDs; nothing is sent."""
    from shadowfenced import engine

    decision = engine.gate('{"password": "s3cr3t-value-here-99"}', policy,
                           registry, mode="send")
    assert decision.decision in ("BLOCK", "HOLD")
    assert decision.sent_text is None


def test_json_api_key_value_held(policy, registry):
    """STOPPED: a key-like value under a JSON key HOLDs; nothing is sent."""
    from shadowfenced import engine

    decision = engine.gate('{"api_key": "k3y-value-abcdef-123456"}', policy,
                           registry, mode="send")
    assert decision.decision in ("BLOCK", "HOLD")
    assert decision.sent_text is None


def test_env_assignment_redacted(policy, registry):
    """STOPPED: ``DB_PASSWORD=value`` redacts the value, keeps the label."""
    from shadowfenced import engine

    decision = engine.gate("DB_PASSWORD=s3cr3t-value-here-99", policy,
                           registry, mode="send")
    assert decision.decision == "REDACT"
    assert decision.sent_text == "DB_PASSWORD=[SECRET_1]"


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: docx metadata (author etc.) is never extracted",
)
def test_docx_author_email_scanned(policy, registry):
    """A mailbox in the docx author field must not scan CLEAN."""
    from docx import Document

    document = Document()
    document.add_paragraph("Quarterly planning notes, nothing sensitive.")
    document.core_properties.author = f"Jon Smith <{email()}>"
    buffer = BytesIO()
    document.save(buffer)
    result = fileguard.scan_upload("plan.docx", buffer.getvalue(), policy,
                                   registry)
    assert result["decision"] != "CLEAN"


def test_pdf_with_only_metadata_stays_unscannable(policy, registry):
    """STOPPED (fail closed): a textless pdf never passes as CLEAN."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    writer.add_metadata({"/Author": email(), "/Title": "notes"})
    buffer = BytesIO()
    writer.write(buffer)
    result = fileguard.scan_upload("notes.pdf", buffer.getvalue(), policy,
                                   registry)
    assert result["decision"] == "UNSCANNABLE"
