"""Red-team: placeholder spoofing (T79).

User text can already contain ``[PII_EMAIL_1]``-style tokens -- pasted from
an earlier answer, or planted by a hostile page. Alone they are harmless:
with no mapping they echo back unrestored. The gap opens when a spoofed
token shares a message with a real value: both end up as the same
``[PII_EMAIL_1]``, so the model's echo restores the victim's value into
attack text on display. Real redactions must never collide with
user-supplied look-alikes.
"""

from __future__ import annotations

import pytest

from shadowfenced.app import SendRequest, send
from helpers_rt import email, upstream_mock


@pytest.fixture()
def mock_upstream():
    yield from upstream_mock()


def test_spoofed_token_alone_echoes_back_unrestored(mock_upstream):
    """STOPPED: a lone spoofed token has no mapping, so nothing restores."""
    mock_upstream(answer="ok [PII_EMAIL_1]")
    body = send(SendRequest(messages=[{"role": "user", "content": (
        "my mail is [PII_EMAIL_1] please use it")}]))
    assert body["decision"] == "ALLOW"
    assert body["answer"] == "ok [PII_EMAIL_1]"


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: spoofed tokens collide with real redaction numbering",
)
def test_spoofed_token_cannot_borrow_real_value(mock_upstream):
    """A spoofed token next to a real email must stay distinct from it."""
    mock_upstream(answer="ok")
    body = send(SendRequest(messages=[{"role": "user", "content": (
        f"real {email()} and pasted [PII_EMAIL_1] here")}]))
    assert body["decision"] == "REDACT"
    assert email() not in (body["sent_text"] or "")
    # The pasted look-alike must not share the real placeholder's number.
    assert (body["sent_text"] or "").count("[PII_EMAIL_1]") <= 1
