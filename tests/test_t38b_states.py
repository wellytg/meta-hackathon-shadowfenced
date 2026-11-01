"""T38b UI state model: square style kept, every control works state by state.

Static checks pin the contract in docs/UI_STATES.md to the shipped
markup, styles and scripts (no browser needed): square corners, the send
state machine, failure keeps the typed text, inline one-line errors, mic
states, text-labelled receipts, stick-to-bottom, Escape order, and the
pure state helpers exported for tests.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"


def _html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _css(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# --- square Windows 3.11 style -----------------------------------------------


def test_no_rounded_corners_anywhere():
    for name in ("style.css", "providers.css", "rules.css", "reid.css",
                 "dictation.css"):
        css = _css(name)
        for value in re.findall(r"border-radius\s*:\s*([^;]+);", css):
            assert value.strip() == "0", f"{name}: {value!r}"


def test_composer_box_is_square_with_hard_outline():
    css = _css("style.css")
    assert "border-radius: 0" in css
    assert "border: 2px solid var(--line)" in css
    assert "One square composer box" in css


# --- frozen hooks --------------------------------------------------------------


def test_ids_and_testids_kept():
    html = _html()
    for hook in (
        'data-testid="composer"', 'data-testid="send"',
        'data-testid="attach"', 'data-testid="composer-tools"',
        'data-testid="provider-select"', 'data-testid="new-chat"',
        'data-testid="chat-list"', 'data-testid="jump-latest"',
        'data-testid="details-drawer"',
        'data-testid="providers-open"', 'data-testid="providers-panel"',
        'data-testid="developer-mode"', 'data-testid="log-ok"',
        'data-testid="log-view"', 'data-testid="log-events"',
        'data-testid="ext-token"', 'data-testid="ext-token-show"',
        'data-testid="ext-token-regenerate"', 'data-testid="empty-hint"',
        'data-testid="status-indicator"', 'data-testid="header-pill"',
        'id="composer-error"',
        'id="checking"', 'id="thread"', 'id="issues"',
    ):
        assert hook in html, hook
    js = _js()
    for hook in ('data-testid", "receipt"', 'data-testid", "banner"', 'data-testid", "receipt-notes"'):
        assert hook in js, hook


# --- send state machine ---------------------------------------------------------


def test_enter_sends_shift_enter_newlines():
    js = _js()
    assert '"Enter"' in js
    assert "event.shiftKey" in js
    assert "preventDefault" in js
    assert "event.isComposing" in js


def test_send_disabled_while_empty_or_busy_with_checking_state():
    js, html = _js(), _html()
    assert "paintSend" in js
    assert "sendBtn.disabled = sending" in js
    assert "if (sending) return;" in js
    assert "disabled>Send</button>" in html
    assert "Checking locally" in html


def test_failure_keeps_typed_text_and_names_it_inline():
    js = _js()
    # The only clear of the composer happens on the success path, before
    # the failure branch; the failure branch only reports inline.
    assert js.index("clearComposer();") < js.index('noteTurn("UNKNOWN", null);')
    tail = js.split('noteTurn("UNKNOWN", null);')[1].split("})")[0]
    assert "showComposerError(" in tail
    assert "clearComposer" not in tail
    assert "Your text was kept" in js


def test_inline_error_is_one_line_and_clears_on_next_action():
    html, css, js = _html(), _css("style.css"), _js()
    assert 'id="composer-error"' in html
    assert 'role="status"' in html
    assert "white-space: nowrap" in css
    assert "text-overflow: ellipsis" in css
    assert js.count("clearComposerError();") >= 3  # send, type, attach


# --- microphone states ------------------------------------------------------------


def test_mic_listening_stop_transcribing_at_cursor():
    js = (STATIC / "dictation.js").read_text(encoding="utf-8")
    assert '"Listening "' in js
    assert '"Stop "' in js
    assert "Transcribing" in js
    assert "setRangeText" in js  # inserted at the cursor
    assert 'dispatchEvent(new Event("input"' in js  # underlines appear
    assert "sf-dictation-consent" in js  # first-run consent


# --- receipts, hold card, choices ------------------------------------------------------


def test_receipts_always_carry_a_text_label():
    js = _js()
    assert "label.textContent = receiptLabel(decision, body);" in js
    for label in ("Sent", "sensitive details protected", "Review required",
                  "Blocked", "couldn't check"):
        assert label in js
    css = _css("style.css")
    assert ".receipt-label" in css


def test_hold_card_disables_choice_after_one_click():
    js = _js()
    assert "approveBtn.disabled = true" in js
    assert "denyBtn.disabled = true" in js
    assert "Send without flagged part" in js
    assert "Don't send" in js


# --- thread stickiness -------------------------------------------------------------------


def test_newest_turn_stays_in_view_unless_scrolled_up():
    js = _js()
    assert "stickToBottom" in js
    assert "scrollThreadToBottom" in js
    assert "Jump to latest" in _html()


# --- keyboard + Escape ----------------------------------------------------------------------


def test_escape_closes_panels_in_order():
    js = _js()
    assert "providers-panel" in js and '"Escape"' in js
    assert "settings-menu" in js
    assert "show-chats" in js and "show-details" in js
    rules = (STATIC / "rules.js").read_text(encoding="utf-8")
    assert '"Escape"' in rules
    mic = (STATIC / "dictation.js").read_text(encoding="utf-8")
    assert '"Escape"' in mic
    providers = (STATIC / "providers.js").read_text(encoding="utf-8")
    assert '"Escape"' in providers


def test_focus_is_visible_and_reachable():
    css = _css("style.css")
    assert "focus-visible" in css
    html = _html()
    assert "skip-link" in html
    assert "tabindex" not in html  # plain DOM order, nothing removed


# --- busy / error states -----------------------------------------------------------------------


def test_log_viewer_and_pairing_have_busy_states():
    js = _js()
    assert "Loading" in js
    assert "/api/log/events?limit=50" in js
    assert "regenBtn.disabled = true" in js
    assert "New token ready. Update the extension." in js
    assert "Could not regenerate the token." in js


def test_provider_cards_have_busy_and_error_states():
    js = (STATIC / "providers.js").read_text(encoding="utf-8")
    assert "Testing" in js
    assert "Could not save the key." in js
    assert "Test failed." in js
    assert "Could not disconnect." in js


# --- exported state helpers ----------------------------------------------------------------------


def test_pure_state_helpers_are_exported():
    assert "window.ShadowfencedUI" in _js()
    for helper in ("receiptLabel", "receiptIcon", "membraneFor",
                   "isSendEnabled"):
        assert helper in _js()
    mic = (STATIC / "dictation.js").read_text(encoding="utf-8")
    assert "window.ShadowfencedDictation" in mic
    assert "formatElapsed" in mic
