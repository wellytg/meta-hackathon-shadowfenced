"""T20 splash page: /welcome/ serves the explainer and the console keeps
its recorder hooks."""

from __future__ import annotations

from pathlib import Path

from shadowfenced.app import app

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"

# Hooks the demo recorders rely on: kept present, visible and clickable.
REQUIRED_HOOKS = (
    "composer",
    "send",
    "answer",
    "banner",
    "btn-why",
    "btn-approve",
    "btn-deny",
    "evidence-card",
    "file-input",
    "ai-saw-toggle",
)


def _static_mount():
    """The StaticFiles mount at / registered by the real application."""
    for route in app.routes:
        if route.__class__.__name__ == "Mount" and getattr(route, "path", "") in ("", "/"):
            return route.app
    raise AssertionError("no static mount found on app")


def _serve(path: str) -> tuple[int, dict, bytes]:
    """Resolve a static path exactly as the mounted StaticFiles(html=True)
    would: directory traversal is contained, and a trailing slash serves
    the folder's index.html. Runs with plain file IO because this sandbox
    forbids the sockets an ASGI request would need."""
    static = _static_mount()
    assert static.__class__.__name__ == "StaticFiles"
    assert static.html, "static mount must serve index.html for directories"
    root = Path(static.directory)
    assert root == STATIC, f"static mount serves {root}, expected {STATIC}"

    clean = path.lstrip("/")
    if ".." in clean.split("/"):
        return 404, {}, b""
    target = root / clean
    if path.endswith("/") or target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        return 404, {}, b""
    content_type = "text/html" if target.suffix == ".html" else "application/octet-stream"
    return 200, {"content-type": content_type}, target.read_bytes()


def test_welcome_page_returns_200_and_links_to_console():
    status, headers, raw = _serve("/welcome/")
    assert status == 200, status
    assert "text/html" in headers.get("content-type", "")
    body = raw.decode("utf-8")
    assert 'href="/"' in body
    assert "console" in body.lower()


def test_welcome_page_explains_product_with_illustration():
    _, _, raw = _serve("/welcome/")
    body = raw.decode("utf-8")
    assert "<svg" in body
    assert "[PII_EMAIL_1]" in body
    assert "Open the console" in body


def test_console_index_keeps_every_recorder_hook():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for hook in REQUIRED_HOOKS:
        assert hook in html, f"missing hook: {hook}"
    # The hooks a user touches first are real elements, not just words.
    for element_id in ("composer", "send", "file-input"):
        assert f'id="{element_id}"' in html


def test_console_index_links_to_welcome():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'href="/welcome/"' in html


def test_console_js_still_builds_dynamic_hooks():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for hook in ("answer", "banner", "btn-why", "btn-approve", "btn-deny",
                 "evidence-card", "ai-saw-toggle"):
        assert hook in js, f"app.js no longer creates: {hook}"
