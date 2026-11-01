"""T38 standard chat look: composer, status, phone panels, log, pairing.

Backend endpoints are plain ``def`` (like the main API), so tests call
them directly with request models -- no sockets, no network. Static
checks pin the composer slots, the header status indicator, the
<=640px slide-out panels, and the drawer log/pairing hooks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from conftest import route_methods
from shadowfenced import config as config_module
from shadowfenced import ecs_log
from shadowfenced import ext_api
from shadowfenced.app import app, guard_status, log_events
from shadowfenced.canary import CanaryRegistry
from shadowfenced.engine import gate
from shadowfenced.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"

SEED = "test-seed-t38"
KINDS = ("db_password", "api_key")
EMAIL = "ada@corvanelabs.com"
OLD_TOKEN = "t38-old-token-value"


def _html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


@pytest.fixture()
def _token_isolation(monkeypatch, tmp_path):
    """Known pairing token plus a temp token file; no leaked override."""
    monkeypatch.setenv("SHADOWFENCED_EXT_TOKEN", OLD_TOKEN)
    monkeypatch.setenv("SF_EXT_TOKEN_FILE", str(tmp_path / "ext_token"))
    config_module.reset_ext_token_override()
    yield
    config_module.reset_ext_token_override()


def _req(token=None, client=("testclient", 50000),
         path="/api/ext/pairing/regenerate") -> Request:
    headers = []
    if token:
        headers.append((b"x-ext-token", token.encode()))
    return Request({
        "type": "http",
        "method": "POST",
        "path": path,
        "scheme": "http",
        "server": ("127.0.0.1", 8765),
        "client": client,
        "headers": headers,
        "query_string": b"",
    })


def _log_two_decisions() -> tuple[str, str]:
    """Append one REDACT and one BLOCK; return (email, canary)."""
    policy = load_policy(ROOT / "policy.yaml")
    registry = CanaryRegistry(SEED, KINDS)
    canary = registry.value("db_password")
    redact_text = f"mail {EMAIL} now"
    block_text = f"password: {canary}"
    ecs_log.append_decision(
        None, gate(redact_text, policy, registry), redact_text)
    ecs_log.append_decision(
        None, gate(block_text, policy, registry), block_text)
    return EMAIL, canary


# --- GET /api/log/events ----------------------------------------------------


def test_log_events_empty_log_is_ok():
    body = log_events(50)
    assert body == {"ok": True, "events": []}


def test_log_events_fields_only():
    _log_two_decisions()
    body = log_events(50)
    assert body["ok"] is True
    assert len(body["events"]) == 2
    for entry in body["events"]:
        assert set(entry) == {
            "ts", "decision_id", "decision", "tier",
            "reasons", "fingerprints",
        }
        assert entry["decision"] in ("ALLOW", "REDACT", "BLOCK", "HOLD")
        assert isinstance(entry["reasons"], list) and entry["reasons"]
        assert isinstance(entry["fingerprints"], list)
        for fingerprint in entry["fingerprints"]:
            assert re.fullmatch(r"[0-9a-f]{12}", fingerprint), fingerprint


def test_log_events_contain_no_raw_values():
    email, canary = _log_two_decisions()
    body = log_events(50)
    dumped = json.dumps(body)
    assert email not in dumped
    assert canary not in dumped
    assert "@" not in json.dumps(
        [entry["reasons"] for entry in body["events"]])


def test_log_events_limit_is_respected():
    _log_two_decisions()
    body = log_events(1)
    assert body["ok"] is True
    assert len(body["events"]) == 1
    assert body["events"][0]["decision"] == "BLOCK"


def test_log_events_reports_broken_chain():
    _log_two_decisions()
    path = Path(ecs_log.resolve_path(None))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json}\n")
    body = log_events(50)
    assert body["ok"] is False


def test_log_events_route_registered():
    table = route_methods(app)
    assert table["/api/log/events"] == ["GET"]
    assert table["/api/guard"] == ["GET"]
    assert table["/api/ext/pairing/regenerate"] == ["POST"]


# --- GET /api/guard ----------------------------------------------------------


def test_guard_status_shape():
    body = guard_status()
    assert body["guard"] == "on"
    assert isinstance(body["glimmer"], bool)


def test_guard_status_never_raises_without_a_model(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")
    body = guard_status()
    assert body == {"guard": "on", "glimmer": False}


# --- POST /api/ext/pairing/regenerate ----------------------------------------


def test_pairing_regenerate_invalidates_old_token(_token_isolation):
    assert ext_api.expected_token() == OLD_TOKEN
    body = ext_api.pairing_regenerate(_req(token=OLD_TOKEN), None)
    new_token = body["token"]
    assert new_token and new_token != OLD_TOKEN
    assert ext_api.expected_token() == new_token
    with pytest.raises(HTTPException) as exc_info:
        ext_api.check_token(_req(token=OLD_TOKEN))
    assert exc_info.value.status_code == 401
    ext_api.check_token(_req(token=new_token))


def test_pairing_regenerate_is_local_only(_token_isolation):
    with pytest.raises(HTTPException) as exc_info:
        ext_api.pairing_regenerate(
            _req(token=OLD_TOKEN, client=("203.0.113.7", 443)), None)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "shadowfenced_ext_local_only"


# --- composer structure -------------------------------------------------------


def test_composer_attach_left_tools_right():
    html = _html()
    attach = html.index('data-testid="attach"')
    tools = html.index('data-testid="composer-tools"')
    send = html.index('data-testid="send"')
    assert html.index("composer-row") < attach < tools < send
    assert html.index("composer-box") < attach
    assert "composer-box" in html
    assert "max-width: 760px" in _css() or "max-width:760px" in _css()


def test_composer_enter_sends_and_send_starts_disabled():
    html = _html()
    assert "disabled>Send</button>" in html
    js = _js()
    assert '"Enter"' in js
    assert "event.shiftKey" in js
    assert "preventDefault" in js
    assert "paintSend" in js
    assert "sendBtn.disabled = sending" in js


def test_dictation_status_stays_a_small_line_in_the_box():
    css = (STATIC / "dictation.css").read_text(encoding="utf-8")
    assert ".sf-dictation-msg" in css
    assert "font-size: 12px" in css


# --- header status -------------------------------------------------------------


def test_header_status_indicator():
    html = _html()
    assert 'data-testid="model-status"' in html
    assert "Model: Muse Spark · cloud" in html
    assert 'data-testid="guard-dot"' in html
    assert "Guard: on · checking locally (code + Muse Glimmer)" in html
    js = _js()
    assert '"/api/guard"' in js
    assert "Guard: on \\u00b7 code checks only" in js
    assert "Muse Spark \\u00b7 cloud" in js


def test_settings_keeps_detail_switch_and_future_developer_mode():
    html = _html()
    assert 'data-testid="developer-mode"' in html
    match = re.search(
        r"<button[^>]*data-testid=\"developer-mode\"[^>]*>", html)
    assert match is not None and "disabled" in match.group(0)
    assert "Coming soon" in html
    assert "Show more detail" in (STATIC / "rules.js").read_text(
        encoding="utf-8")


# --- phone layout ---------------------------------------------------------------


def test_phone_panels_slide_out():
    html = _html()
    css = _css()
    assert "@media (max-width: 640px)" in css
    assert 'id="chats-toggle"' in html
    assert 'aria-controls="sessions-panel"' in html
    assert 'id="sessions-panel"' in html
    for hook in ("sessions-panel", "show-chats", "show-details",
                 "translateX(-105%)", "translateX(105%)"):
        assert hook in css, hook
    # Nothing stacks under the composer anymore.
    assert ".sessions { order" not in css
    assert "max-height: 20vh" not in css
    assert "max-height: 28vh" not in css


def test_membrane_labels_are_gone():
    html = _html()
    css = _css()
    for banned in ("membrane-caption", "zone-membrane", "Private",
                   "External", "PRIVATE", "EXTERNAL"):
        assert banned not in html, banned
        assert banned not in css, banned


# --- drawer log viewer + pairing --------------------------------------------------


def test_log_viewer_hooks():
    html = _html()
    assert 'data-testid="log-view"' in html
    assert "View log" in html
    assert 'data-testid="log-events"' in html
    assert "/api/log/events?limit=50" in _js()


def test_pairing_hooks_and_explainer():
    html = _html()
    assert "Paste this into the Shadowfenced browser extension. " \
        "It lets only your extension use this app." in html
    assert 'data-testid="ext-token-show"' in html
    assert 'data-testid="ext-token-regenerate"' in html
    assert "/api/ext/pairing/regenerate" in _js()
