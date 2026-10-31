"""Upload API tests (T12): flagged csv redacts on send, unscannable HOLDs."""

from __future__ import annotations

import json

import httpx
import pytest

from shadowfenced import holds, uploads
from shadowfenced.app import SendRequest, app, handle_upload, send


@pytest.fixture(autouse=True)
def _clean_state():
    uploads.reset_for_tests()
    holds.reset_for_tests()
    yield
    uploads.reset_for_tests()
    holds.reset_for_tests()


@pytest.fixture()
def mock_upstream():
    calls: list[httpx.Request] = []

    def make(answer: str = "noted [PII_EMAIL_1], thanks") -> list:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": answer}}
                    ]
                },
            )

        app.state.upstream_transport = httpx.MockTransport(handler)
        return calls

    yield make
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")


CSV = (
    b"name,email,phone\n"
    b"Ada Lark,ada@corvanelabs.com,+1 (415) 555-0132\n"
)


def test_flagged_csv_redacts_on_send(mock_upstream):
    calls = mock_upstream("noted [PII_EMAIL_1], thanks")
    uploaded = handle_upload("customers.csv", CSV)
    assert uploaded["status"] == "flagged"
    assert uploaded["summary"].get("pii", 0) >= 2  # email + phone
    assert uploaded["summary_text"]
    assert uploaded["spans"]
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "summarise this"}],
            file_ids=[uploaded["file_id"]],
        )
    )
    assert body["decision"] == "REDACT"
    assert body["sent_text"] is not None
    forwarded = json.dumps(json.loads(calls[0].content.decode("utf-8")))
    assert "Attached file customers.csv:" in body["sent_text"]
    assert "ada@corvanelabs.com" not in body["sent_text"]
    assert "[PII_EMAIL_1]" in body["sent_text"]
    assert "ada@corvanelabs.com" not in forwarded
    # Placeholders are shared between the message and the file text.
    assert "[PII_EMAIL_1]" in forwarded
    assert body["answer"] == "noted ada@corvanelabs.com, thanks"


def test_unscannable_file_holds_send(mock_upstream):
    calls = mock_upstream()
    uploaded = handle_upload("broken.docx", b"not a zip file")
    assert uploaded["status"] == "unscannable"
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "summarise this"}],
            file_ids=[uploaded["file_id"]],
        )
    )
    assert body["decision"] == "HOLD"
    assert body["sent_text"] is None
    assert body["answer"] is None
    assert body["review_url"].startswith("/api/holds/")
    assert calls == []


def test_unknown_file_id_holds_send(mock_upstream):
    calls = mock_upstream()
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "hello"}],
            file_ids=["no-such-file"],
        )
    )
    assert body["decision"] == "HOLD"
    assert calls == []


def test_clean_file_sends_with_attachment(mock_upstream):
    calls = mock_upstream("done")
    uploaded = handle_upload("notes.md", b"water the plants")
    assert uploaded["status"] == "clean"
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "read this"}],
            file_ids=[uploaded["file_id"]],
        )
    )
    assert body["decision"] == "ALLOW"
    forwarded = json.loads(calls[0].content.decode("utf-8"))
    assert "Attached file notes.md:" in forwarded["messages"][-1]["content"]
