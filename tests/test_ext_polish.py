"""Extension polish tests (T42): build flavours, icons, popup, cards, test page.

- The release build must mention only loopback + the four chat sites.
- The dev build (``build.py --dev``) additionally matches the test page.
- Icons exist as SVG source plus 16/32/48/128 px PNGs with exact sizes.
- The popup has its title bar, 3-state status, Paste/Save, four site
  switches and the Test button at ~340px with no rounded corners.
- In-page cards live in a closed shadow root with focus/Escape handling.
- The local test page imitates a chat composer.
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"
SRC = EXT / "src"

EXPECTED_HOSTS = {
    "http://127.0.0.1:8765/*",
    "https://chatgpt.com/*",
    "https://claude.ai/*",
    "https://gemini.google.com/*",
    "https://meta.ai/*",
    "https://www.meta.ai/*",
}
FOUR_SITES = ("chatgpt.com", "claude.ai", "gemini.google.com", "meta.ai")
ICON_SIZES = (16, 32, 48, 128)


def _build(dev: bool = False) -> None:
    cmd = [sys.executable, str(EXT / "build.py")]
    if dev:
        cmd.append("--dev")
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, f"build.py{' --dev' if dev else ''}: {proc.stderr}"


def _dist_manifest(name: str) -> dict:
    return json.loads((EXT / "dist" / name / "manifest.json").read_text(encoding="utf-8"))


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name}: bad PNG signature"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def test_release_build_has_only_four_sites_plus_loopback():
    _build(dev=False)
    for name in ("chrome", "firefox"):
        manifest = _dist_manifest(name)
        assert set(manifest["host_permissions"]) == EXPECTED_HOSTS, name
        matches = [m for block in manifest["content_scripts"] for m in block["matches"]]
        for site in FOUR_SITES:
            assert any(site in m for m in matches), f"{name} missing {site}"
        for m in matches:
            assert "8000" not in m and "testpage" not in m and "localhost" not in m, (
                f"{name}: release match mentions the test page: {m}"
            )
        assert "(dev)" not in manifest.get("name", ""), name


def test_release_source_manifests_mention_no_test_page():
    for name in ("manifest_chrome.json", "manifest_firefox.json"):
        text = (EXT / name).read_text(encoding="utf-8")
        assert "8000" not in text and "testpage" not in text and "localhost" not in text, name


def test_dev_build_adds_test_page_only():
    _build(dev=True)
    for name in ("chrome-dev", "firefox-dev"):
        manifest = _dist_manifest(name)
        matches = [m for block in manifest["content_scripts"] for m in block["matches"]]
        assert "http://127.0.0.1:8000/*" in matches, f"{name}: no test-page match"
        assert "http://127.0.0.1:8000/*" in manifest["host_permissions"], name
        for site in FOUR_SITES:
            assert any(site in m for m in matches), f"{name} missing {site}"


def test_icons_present_at_all_sizes():
    svg = SRC / "icons" / "icon.svg"
    assert svg.is_file(), "src/icons/icon.svg missing"
    assert "<svg" in svg.read_text(encoding="utf-8"), "icon.svg has no <svg>"
    for size in ICON_SIZES:
        png = SRC / "icons" / f"icon{size}.png"
        assert png.is_file(), f"src/icons/icon{size}.png missing"
        assert _png_size(png) == (size, size), f"icon{size}.png wrong size"
    for name in ("manifest_chrome.json", "manifest_firefox.json"):
        manifest = json.loads((EXT / name).read_text(encoding="utf-8"))
        icons = manifest.get("icons", {})
        for size in ICON_SIZES:
            rel = icons.get(str(size), "")
            assert rel, f"{name}: no {size}px icon"
            assert (EXT / rel).is_file(), f"{name}: missing {rel}"


def test_build_ships_icons():
    _build(dev=False)
    for size in ICON_SIZES:
        assert (EXT / "dist" / "chrome" / "src" / "icons" / f"icon{size}.png").is_file()


def test_popup_has_required_ui():
    html = (SRC / "popup.html").read_text(encoding="utf-8")
    assert "Shadowfenced" in html
    assert 'id="status"' in html and 'id="statusIcon"' in html
    assert 'id="paste"' in html and 'id="save"' in html
    assert 'id="test"' in html and 'id="testResult"' in html
    for site in ("chatgpt", "claude", "gemini", "meta"):
        assert f'data-site="{site}"' in html, f"popup missing switch {site}"
    js = (SRC / "popup.js").read_text(encoding="utf-8")
    assert "SF_HEALTH" in js and "SF_SCAN" in js, "popup never probes the local app"
    assert "Not paired" in js and "Not running" in js and "Connected" in js
    css = (SRC / "styles.css").read_text(encoding="utf-8")
    assert "340px" in css, "popup width should be about 340px"
    assert "border-radius:" not in css, "square style: no rounded corners"


def test_content_cards_in_closed_shadow_root():
    js = (SRC / "content.js").read_text(encoding="utf-8")
    assert "attachShadow" in js and "closed" in js, "cards must live in a closed shadow root"
    assert "appendChild" in js
    assert "Escape" in js, "cards must close on Escape"
    assert "tabindex" in js, "cards/underlines must take keyboard focus"
    assert "Replace with placeholder" in js and "Ignore" in js
    assert "Blocked" in js and "not sent" in js
    assert "Send anyway" in js and "Cancel" in js
    assert "isn't running" in js, "not-running card missing"
    assert "details protected" in js or "detail" in js
    assert "wavy" in js, "underlines must be wavy or dotted state lines"
    assert "border-radius" not in js, "square style: no rounded corners"


def test_testpage_imitates_chat_composer():
    page = EXT / "testpage" / "index.html"
    assert page.is_file(), "extension/testpage/index.html missing"
    html = page.read_text(encoding="utf-8")
    assert "<textarea" in html and 'id="composer-text"' in html
    assert 'id="composer-edit"' in html and "contenteditable" in html
    assert 'id="sendBtn"' in html
    assert 'id="replyArea"' in html
    for url in re.findall(r"https?://[^\s'\"<>]+", html):
        assert url.startswith("http://127.0.0.1"), (
            f"test page must not depend on remote URLs: {url}"
        )
