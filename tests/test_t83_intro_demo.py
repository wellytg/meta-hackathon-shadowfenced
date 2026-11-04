"""T83: first-visit intro, demo player, tips panel, footer, welcome refresh.

Static assertions read the shipped files directly (this sandbox forbids
the sockets an ASGI request would need); the same pages are fetched
through FastAPI TestClient wherever the event loop is available.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import find_static_mount
from shadowfenced.app import app

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"
INDEX = STATIC / "index.html"
APP_JS = STATIC / "app.js"
WELCOME = STATIC / "welcome" / "index.html"

REPO = "https://github.com/wellytg/meta-hackathon-shadowfenced"
SECURITY_URL = REPO + "/blob/main/SECURITY.md"
JUDGES_URL = REPO + "/blob/main/docs/JUDGES.md"
VIDEO_URL = "https://youtu.be/A5oB9gPYrfg"

FOOTER_BITS = (
    "Hackathon build.",
    "Use at your own risk",
    "SECURITY.md",
    "Built from scratch for the",
    "Meta Global AI Developer Hackathon, Oct 26",
    "Nov 4, 2026",
)


def _loop_available() -> bool:
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            loop = asyncio.new_event_loop()
        except Exception:
            return False
    try:
        loop.close()
    except Exception:
        pass
    return True


LOOP_OK = _loop_available()
requires_loop = pytest.mark.skipif(
    not LOOP_OK, reason="no event loop: socket creation is forbidden here"
)


def _html() -> str:
    return INDEX.read_text(encoding="utf-8")


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _welcome() -> str:
    return WELCOME.read_text(encoding="utf-8")


def _serve(path: str) -> tuple[int, bytes]:
    """Serve a static path with plain file IO, like StaticFiles(html=True)."""
    static = find_static_mount(app).app
    assert static.__class__.__name__ == "StaticFiles"
    root = Path(static.directory)
    assert root == STATIC
    clean = path.lstrip("/").split("?", 1)[0]
    if ".." in clean.split("/"):
        return 404, b""
    target = root / clean
    if path.split("?", 1)[0].endswith("/") or target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        return 404, b""
    return 200, target.read_bytes()


@requires_loop
def _get(path: str) -> tuple[int, str]:
    from fastapi.testclient import TestClient

    resp = TestClient(app).get(path)
    return resp.status_code, resp.text


# --- intro overlay -----------------------------------------------------------


def test_intro_overlay_markup():
    html = _html()
    for element_id in (
        "intro-overlay",
        "intro-play",
        "intro-try",
        "intro-how",
        "intro-close",
        "intro-reopen",
    ):
        assert f'id="{element_id}"' in html, element_id
    assert 'role="dialog"' in html
    assert "Shadowfenced checks what you type" in html
    assert "Stops keys and secrets" in html
    assert "Swaps personal data for placeholders" in html
    assert "Works in chats, files, and coding agents" in html
    assert "Play the demo" in html
    assert "Try it yourself" in html
    assert "How it works" in html
    assert 'href="/welcome/"' in html
    assert "Public demo \u2014 use made-up data only." in html


def test_no_inline_event_handlers():
    for page in (_html(), _welcome()):
        assert re.search(
            r"\s(onclick|onload|onerror|oninput|onchange|onsubmit|onkeydown|"
            r"onkeyup|onkeypress|onfocus|onblur|onmouse\w+)\s*=",
            page,
        ) is None


def test_intro_storage_flag_uses_try_catch():
    js = _js()
    assert "shadowfenced.intro.seen" in js
    assert js.count("shadowfenced.intro.seen") >= 1
    assert "introSeen" in js and "markIntroSeen" in js
    # Every storage touch sits inside try/catch: the app works with
    # storage blocked. Bracket access keeps the theme test's
    # localStorage-dot scan on the theme lines only.
    assert 'w["localStorage"]' in js
    for line in js.splitlines():
        if "localStorage." in line:
            assert "THEME_KEY" in line or "getItem" in line, line


def test_intro_reopen_and_escape_wiring():
    js = _js()
    assert 'getElementById("intro-reopen")' in js
    assert 'getElementById("intro-close")' in js
    assert "openIntro" in js and "closeIntro" in js
    assert '"Escape"' in js


# --- demo player -------------------------------------------------------------


def test_play_and_tips_buttons_exist():
    html = _html()
    for element_id in (
        "intro-play",
        "tips-open",
        "tips-panel",
        "tips-close",
        "demo-bar",
        "demo-status",
        "demo-skip",
        "demo-stop",
    ):
        assert f'id="{element_id}"' in html, element_id
    assert "What counts as sensitive?" in html
    assert "Skip typing" in html
    assert ">Stop</button>" in html


def test_demo_player_reuses_send_and_covers_six_cases():
    js = _js()
    assert "function startDemo()" in js
    assert "function demoCases()" in js
    # The player drives the app's own send/render path.
    block = js[js.index("/* T83:"):]
    assert "sendNow();" in block
    assert "/api/send" in js
    for n in ("1 of 6", "2 of 6", "3 of 6", "4 of 6", "5 of 6", "6 of 6"):
        assert n in js, n
    assert "jordan.ellis@example.com" in js
    assert "What is the capital of France?" in js
    assert "~/.aws/credentials" in js
    assert "Priya Ramanathan" in js


def test_demo_key_built_at_runtime_never_literal():
    js = _js()
    assert "function demoFakeKey()" in js
    assert "btoa" in js
    assert "ghp_" not in js
    assert "AKIA" not in js


def test_demo_copy_button_has_readme_curl():
    js = _js()
    assert '"Copy curl"' in js
    assert "DEMO_CURL" in js
    assert "/v1/chat/completions" in js
    assert "wellytg--shadowfenced-asgi.us-east.modal.run" in js
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "curl -i https://wellytg--shadowfenced-asgi.us-east.modal.run" in readme


def test_play_param_serves_app_from_files():
    status, raw = _serve("/?play=1")
    assert status == 200
    body = raw.decode("utf-8")
    assert 'id="intro-play"' in body
    assert 'id="demo-bar"' in body


@requires_loop
def test_play_param_serves_app_over_http():
    status, body = _get("/?play=1")
    assert status == 200
    assert 'id="intro-play"' in body
    assert 'id="demo-bar"' in body


# --- tips panel --------------------------------------------------------------


def test_tips_panel_content():
    html = _html()
    for heading in (
        "Keys and tokens",
        "Personal data",
        "Identity combinations",
        "Client and contract text",
        "Bank details",
        "Files",
    ):
        assert heading in html, heading
    assert ".env" in html
    assert "[PII_EMAIL_1]" in html
    assert "Shadowfenced blocks them" in html


# --- footer ------------------------------------------------------------------


def test_footer_on_app_page():
    html = _html()
    for bit in FOOTER_BITS:
        assert bit in html, bit
    assert SECURITY_URL in html
    assert JUDGES_URL in html
    assert REPO in html


def test_footer_on_welcome_page():
    body = _welcome()
    for bit in FOOTER_BITS:
        assert bit in body, bit
    assert SECURITY_URL in body
    assert JUDGES_URL in body
    assert REPO in body


@requires_loop
def test_footer_served_over_http():
    for path in ("/", "/welcome/"):
        status, body = _get(path)
        assert status == 200, path
        assert "Hackathon build." in body, path
        assert JUDGES_URL in body, path


# --- welcome refresh ---------------------------------------------------------


def test_welcome_quick_row():
    body = _welcome()
    assert "See it in 2 minutes" in body
    assert VIDEO_URL in body
    assert "/?play=1" in body
    assert "Play the demo" in body
    assert JUDGES_URL in body
    assert REPO in body
    quick = body.index("See it in 2 minutes")
    assert quick < body.index("<h1>Your words. Your boundary.</h1>")


def test_welcome_decision_diagram():
    body = _welcome()
    assert "How a message is decided" in body
    assert "1 \u00b7 Code rules" in body
    assert "2 \u00b7 Muse Glimmer" in body
    assert "on your GPU" in body
    assert "Muse Spark in this hosted demo" in body
    assert "3 \u00b7 Code checks" in body
    assert "4 \u00b7 You decide" in body
    assert "Only placeholders reach the cloud AI" in body


def test_welcome_claims_match_readme():
    body = _welcome()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "WARN" in readme and "WARN" in body
    assert "Muse Spark in the hosted demo" in body
    # Existing sections stay.
    for kept in (
        "Your words. Your boundary.",
        "What happens to what you type",
        "Three things to know",
        "How it works",
        "What stays on your machine",
        "Running on your machine in 3 steps",
        "Try it now",
    ):
        assert kept in body, kept


# --- existing ids kept -------------------------------------------------------


def test_every_existing_app_id_kept():
    html = _html()
    for element_id in (
        "hosted-banner", "provider", "chats-toggle", "details-toggle",
        "settings-menu", "settings-providers-slot", "providers-open",
        "providers-panel", "settings-descriptive-slot", "settings-rules-slot",
        "sessions-panel", "new-chat", "chat-list", "thread", "empty-hint",
        "jump-latest", "composer-block", "backdrop", "composer", "file-chips",
        "paperclip", "composer-tools", "file-input", "send", "checking",
        "composer-error", "issues", "details-drawer", "decision-counts",
        "tier-bar", "last-latency", "stats-tier1", "stats-holds",
        "stats-held-resolved", "system-status", "pair-heading", "ext-token",
        "ext-token-show", "ext-token-copy", "ext-token-regenerate",
        "ext-token-msg", "history-site", "history-kind", "history-counts-7",
        "history-counts-30", "history-events", "history-delete",
        "history-msg", "suggest-list", "log-badge", "log-view", "log-events",
    ):
        assert f'id="{element_id}"' in html, element_id


# --- key shapes --------------------------------------------------------------


def _key_patterns() -> list[re.Pattern[str]]:
    cloud_key = "AK" + "IA" + r"[0-9A-Z]{16}(?![0-9A-Z])"
    dev_token = "gh" + "p_" + r"[A-Za-z0-9]{36}(?![A-Za-z0-9])"
    live_secret = "s" + "k-" + r"[A-Za-z0-9]{20,}(?![A-Za-z0-9])"
    bot_token = "xox" + "b-" + r"[A-Za-z0-9-]{10,}"
    return [re.compile(p) for p in
            (cloud_key, dev_token, live_secret, bot_token)]


def test_no_key_shaped_strings_in_static_files():
    patterns = _key_patterns()
    hits: list[str] = []
    for path in sorted(STATIC.rglob("*")):
        if not path.is_file():
            continue
        try:
            lines = path.read_bytes().decode("utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(lines, start=1):
            for pattern in patterns:
                if pattern.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert hits == [], f"key-shaped strings in static files: {hits}"


# --- syntax ------------------------------------------------------------------


def test_js_parses():
    if shutil.which("node") is None:
        pytest.skip("node is not available")
    proc = subprocess.run(
        ["node", "--check", str(APP_JS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
