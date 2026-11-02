"""T63 live-test wiring checks: selectors, guard/badge markers, status flow.

Companion to ``extension/tests/t63.test.js`` (which does real selector
matching): these assert the source wiring the Node tests cannot load
(content/background/popup need browser APIs) carries the T63 fixes.
"""

from __future__ import annotations

from pathlib import Path

EXT = Path(__file__).resolve().parents[1] / "extension"
SRC = EXT / "src"


def _src(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def test_chatgpt_and_meta_selectors_present():
    adapters = _src("adapters.js")
    assert "#mobile-composer-prompt" in adapters
    assert 'textarea[aria-label*="Chat with ChatGPT" i]' in adapters
    assert 'button[aria-label="Send message"]' in adapters
    assert "#prompt-textarea" in adapters, "signed-in selectors must stay"
    assert 'input[placeholder*="Ask Meta AI" i]' in adapters
    assert 'button[aria-label="Send"]' in adapters


def test_attachment_selectors_match_no_buttons():
    adapters = _src("adapters.js")
    assert '"Upload"' not in adapters, "Upload button selector must go"
    assert '[aria-label*="Attach" i]' not in adapters


def test_content_guard_badge_and_focus_wiring():
    content = _src("content.js")
    assert "not protecting this page" in content
    assert "SF_PROT" in content
    assert "normalizeServerDecision" in content
    assert "bindCaptureGuard" in content
    assert 'button[type="submit"]' in content
    assert '[aria-label*="Send" i]' in content
    assert "shouldFocusCard" in content
    assert "countsAsAttachment" in content
    assert "not sent" in content


def test_background_status_and_protection_wiring():
    background = _src("background.js")
    assert "SF_PROT" in background
    assert "err.status" in background or "e.status" in background
    assert "sfProt" in background


def test_popup_status_and_amber_row_wiring():
    popup = _src("popup.js")
    assert "401" in popup and "403" in popup
    assert "sfProt" in popup
    assert "sf-amber" in popup
    css = _src("styles.css")
    assert "sf-amber" in css
    assert "border-radius:" not in css
