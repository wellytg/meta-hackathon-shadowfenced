"""T31 chat shell: themes, receipts, memory-only chats, landing, docs.

Static checks only (no network): the Python API contract is unchanged, so
behavior is asserted against the shipped static files.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"
INDEX = STATIC / "index.html"
APP_JS = STATIC / "app.js"
STYLE = STATIC / "style.css"
WELCOME = STATIC / "welcome" / "index.html"

THEME_NAMES = ["classic"]

REQUIRED_TESTIDS = [
    "hosted-banner",
    "header-pill",
    "empty-hint",
    "composer",
    "send",
    "file-input",
    "provider-select",
    "stats-tier1",
    "stats-holds",
    "stats-held-resolved",
    "log-ok",
    # T31 additions
    "new-chat",
    "chat-list",
    "details-drawer",
    "receipt",
    # T32 hook points (receipt-notes is runtime-created, like receipt)
    "providers-open",
    "providers-panel",
    "composer-tools",
    "jump-latest",
]


def _html() -> str:
    return INDEX.read_text(encoding="utf-8")


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


# --- single Classic theme (T32: no switcher, no Fluid) -------------------------


def test_single_classic_theme_no_picker():
    html = _html()
    assert 'data-testid="theme-select"' not in html
    assert 'id="theme"' not in html
    assert "themes.css" not in html
    assert 'data-theme="classic"' in html
    assert "fluid" not in html.lower()


def test_theme_name_only_in_local_storage():
    js = _js()
    assert "shadowfenced.theme" in js
    assert "classic" in js
    assert "fluid" not in js
    assert 'setAttribute("data-theme"' in js
    storage_lines = [
        line
        for line in js.splitlines()
        if "localStorage" in line and "localStorage." in line
    ]
    assert storage_lines, "theme persistence must use localStorage"
    for line in storage_lines:
        assert "THEME_KEY" in line or "getItem" in line, line


def test_no_fluid_stylesheet_and_ids_stay_unique():
    assert not (STATIC / "themes.css").exists()
    html = _html()
    for element_id in ("thread", "composer", "send", "chat-list", "provider",
                       "composer-tools", "providers-panel", "providers-open",
                       "jump-latest"):
        assert html.count(f' id="{element_id}"') == 1, element_id


def test_every_required_testid_present():
    html = _html()
    found = set(re.findall(r'data-testid="([^"]+)"', html))
    for hook in REQUIRED_TESTIDS:
        if hook == "receipt":
            continue  # created at runtime per assistant turn
        assert hook in found, hook
    assert 'data-testid", "receipt"' in _js()
    assert 'data-testid", "receipt-notes"' in _js()


# --- memory-only chats --------------------------------------------------------


def test_chats_are_memory_only():
    js = _js()
    assert 'data-testid", "new-chat"' in js or 'getElementById("new-chat")' in js
    assert 'getElementById("chat-list")' in js
    start = js.index("Sessions live in page memory only")
    block = js[start:]
    block = block[: block.index("T31 decision receipts")]
    assert "fetch(" not in block
    assert "localStorage." not in block
    assert "XMLHttpRequest" not in block


# --- receipts + membrane -------------------------------------------------------


def test_receipts_map_every_decision_including_unknown():
    js = _js()
    assert 'data-testid", "receipt"' in js
    assert '"Sent"' in js
    assert '" sensitive details protected"' in js
    assert '"Review required"' in js
    assert '"Blocked"' in js
    # Unknown -> neutral, never "sent".
    assert '"Not sent' in js
    assert '"UNKNOWN"' in js
    assert "KNOWN_DECISIONS" in js


def test_receipts_come_from_the_server_response():
    js = _js()
    assert "body.decision" in js
    assert "renderReceipt(decision, body, banner)" in js
    assert 'renderReceipt("UNKNOWN", null, banner)' in js
    assert "user_message" in js
    assert "resolved_tier" in js
    # No raw values or ids flow into receipts/attrs/membrane.
    receipt_fn = js[js.index("function renderReceipt"):]
    receipt_fn = receipt_fn[: receipt_fn.index("loadHostedBanner();")]
    for banned in ("sent_text", "span_id", "decision_id", "placeholders"):
        assert banned not in receipt_fn, banned


def test_membrane_states_follow_the_server_decision():
    js = _js()
    assert 'data-membrane' in js
    assert '"inspecting"' in js
    for state in ("released", "redacted", "review", "blocked", "unknown"):
        assert f'"{state}"' in js, state
    assert "setMembrane(membraneFor(decision))" in js


def test_provider_picker_stays_server_fed():
    js = _js()
    assert '"/api/providers"' in js
    assert '"/api/provider"' in js
    html = _html()
    assert "topbar-picks" in html
    assert 'data-testid="provider-select"' in html


# --- landing ------------------------------------------------------------------


def test_welcome_hero_story_how_and_trust():
    body = WELCOME.read_text(encoding="utf-8")
    assert "Your words. Your boundary." in body
    for step in ("(a)", "(b)", "(c)"):
        assert step in body, step
    assert body.count("Illustration") >= 3
    assert "With Shadowfenced" in body
    assert "[PII_EMAIL_1]" in body
    for tier in ("Tier 1", "Tier 2"):
        assert tier in body, tier
    for word in ("ALLOW", "REDACT", "BLOCK", "HOLD"):
        assert word in body, word
    assert "What stays on your machine" in body
    assert "Open the app" in body
    assert 'href="/"' in body


# --- T32 hook points ---------------------------------------------------------------


def test_composer_tools_hook_sits_next_to_attach():
    html = _html()
    attach = html.index('data-testid="attach"')
    tools = html.index('data-testid="composer-tools"')
    assert attach < tools
    assert html.index("composer-row") < attach < tools
    assert '<div id="composer-tools"' in html


def test_providers_hook_opens_an_empty_dialog():
    html = _html()
    assert 'id="providers-open"' in html
    assert "Providers" in html
    match = re.search(
        r'<section[^>]*id="providers-panel"[^>]*>\s*</section>', html)
    assert match is not None, "providers panel must be present and empty"
    tag = match.group(0)
    assert 'data-testid="providers-panel"' in tag
    assert 'role="dialog"' in tag


def test_receipt_notes_created_at_runtime():
    js = _js()
    assert '"receipt-notes"' in js
    assert 'data-testid", "receipt-notes"' in js


def test_jump_to_latest_button_starts_hidden():
    html = _html()
    match = re.search(r'<button[^>]*id="jump-latest"[^>]*>', html)
    assert match is not None
    assert "hidden" in match.group(0)
    assert "Jump to latest" in html


# --- T32 scroll container structure ----------------------------------------------------


def test_message_list_is_the_only_scroller():
    html = _html()
    assert html.index('id="thread"') < html.index('id="jump-latest"')
    assert html.index('id="jump-latest"') < html.index('id="composer-block"')
    css = STYLE.read_text(encoding="utf-8")
    assert "overflow: hidden" in css  # the page itself never scrolls
    assert re.search(r"\.thread\s*\{[^}]*overflow-y:\s*auto", css)
    assert re.search(r"\.composer-block\s*\{[^}]*position:\s*sticky", css)
    assert re.search(r"\.drawer-body\s*\{[^}]*overflow-y:\s*auto", css)
    assert "max-height: calc(8 * 1.5em" in css  # composer ~8 lines, then scrolls


def test_auto_scroll_and_composer_growth_wiring():
    js = _js()
    assert "MutationObserver" in js
    assert "stickToBottom" in js
    assert "jump-latest" in js
    assert "fitComposer" in js
    assert "scrollThreadToBottom" in js


# --- welcome page (T32 Classic + install) -------------------------------------------------


def test_welcome_starts_with_the_risk_story_and_install():
    body = WELCOME.read_text(encoding="utf-8")
    assert "You \u2192 AI = risk" in body
    assert body.index("story-lead") < body.index("How it works")
    assert "Install" in body
    for step in ("Clone", "Install", "Run"):
        assert step in body, step
    assert "git clone" in body
    assert "pip install" in body
    assert "uvicorn" in body
    assert "themes.css" not in body
    assert 'data-theme="classic"' in body
    assert "fluid" not in body.lower()


# --- docs ----------------------------------------------------------------------


def test_docs_present():
    inventory = (ROOT / "docs" / "UI_INVENTORY.md").read_text(encoding="utf-8")
    assert "testid" in inventory.lower()
    assert "receipt" in inventory.lower()
    themes = (ROOT / "docs" / "THEMES.md").read_text(encoding="utf-8")
    assert "shadowfenced.theme" in themes
    assert "Classic" in themes
    assert "single theme" in themes
    assert "classic" in themes
    assert "fluid-dark" not in themes
    assert '"fluid"' not in themes


# --- syntax ---------------------------------------------------------------------


def test_js_parses():
    if shutil.which("node") is None:
        import pytest

        pytest.skip("node is not available")
    for target in (APP_JS, STATIC / "rules.js"):
        proc = subprocess.run(
            ["node", "--check", str(target)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
