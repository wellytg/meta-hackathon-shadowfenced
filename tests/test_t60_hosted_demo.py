"""T60 hosted public demo: honest status, no local-only panels, no clipping."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from shadowfenced import ext_api, health as health_module

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"

OLLAMA_TEXT = "Not used in the public demo \u2014 Muse Spark does the Tier 2 check"
KEYSTORE_TEXT = "Not used in the public demo \u2014 the key comes from the host's secret store"
EXT_TEXT = "Not used in the public demo \u2014 the extension pairs with the app on your own PC"
PROVIDERS_HOSTED_NOTICE = (
    "Public demo: uses the host's Muse Spark key. "
    "Run Shadowfenced on your PC to use your own keys."
)
GUARD_HOSTED_LABEL = "Guard: on \\u00b7 code checks + Muse Spark review"


@pytest.fixture()
def hosted_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SF_HOSTED", "1")
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("SF_LOG_PATH", str(tmp_path / "decisions.jsonl"))
    yield


@pytest.fixture()
def local_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SF_HOSTED", raising=False)
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("SF_LOG_PATH", str(tmp_path / "decisions.jsonl"))
    yield


def _rule_block(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"missing CSS rule for {selector}"
    return match.group(1)


def test_hosted_health_reports_off_with_demo_texts(hosted_env):
    ollama = health_module.check_ollama()
    keystore = health_module.check_keystore()
    extension = health_module.check_extension()
    assert ollama == {"name": "ollama", "status": "off", "detail": OLLAMA_TEXT}
    assert keystore == {"name": "keystore", "status": "off", "detail": KEYSTORE_TEXT}
    assert extension == {"name": "extension", "status": "off", "detail": EXT_TEXT}


def test_hosted_health_collective_and_local_unchanged(hosted_env):
    rows = {entry["name"]: entry for entry in health_module.collect_components()}
    assert rows["ollama"]["detail"] == OLLAMA_TEXT
    assert rows["keystore"]["detail"] == KEYSTORE_TEXT
    assert rows["extension"]["detail"] == EXT_TEXT


def test_local_health_unchanged(local_env):
    assert health_module.check_keystore()["status"] in ("ok", "degraded")
    assert health_module.check_extension()["status"] in ("ok", "off")
    assert health_module.check_extension()["detail"] != EXT_TEXT


def test_pairing_route_403_when_hosted(hosted_env):
    with pytest.raises(HTTPException) as caught:
        ext_api.pairing_status(None)
    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "shadowfenced_hosted_pairing_disabled"


def test_pairing_regenerate_403_when_hosted(hosted_env):
    request = Request({
        "type": "http", "method": "POST", "path": "/api/ext/pairing/regenerate",
        "scheme": "http", "server": ("127.0.0.1", 8765),
        "client": ("testclient", 50000), "headers": [], "query_string": b"",
    })
    with pytest.raises(HTTPException) as caught:
        ext_api.pairing_regenerate(request, None)
    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "shadowfenced_hosted_pairing_disabled"


def test_pairing_route_unchanged_locally(local_env, monkeypatch):
    monkeypatch.setenv("SHADOWFENCED_EXT_TOKEN", "local-token-123")
    assert ext_api.pairing_status(None) == {"token": "local-token-123"}


def test_banner_in_normal_flow_not_fixed():
    for css_path in (
        STATIC / "style.css",
        STATIC / "welcome" / "welcome.css",
    ):
        block = _rule_block(css_path.read_text(encoding="utf-8"), "#hosted-banner")
        assert "position: fixed" not in block
        assert "position:fixed" not in block.replace(" ", "")


def test_drawer_body_fills_column_and_scrolls():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "display: flex; flex-direction: column; min-height: 0" in css
    blocks = re.findall(r"\.details-drawer\s*\{([^}]*)\}", css)
    assert blocks, "missing CSS rule for .details-drawer"
    assert any(re.search(r"overflow-y\s*:\s*auto", block) for block in blocks)
    assert any(re.search(r"min-height\s*:\s*0", block) for block in blocks)


def test_off_badge_is_neutral_grey():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    off = _rule_block(css, ".sys-off")
    down = _rule_block(css, ".sys-down")
    assert off != down
    assert "var(--signal)" not in off and "var(--hold)" not in off


def test_hosted_hides_pair_panel_in_ui():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="pair-heading"' in html
    assert "pair-heading" in js and "hidden = true" in js
    assert "hidePairPanel" in js and "applyHostedUi" in js


def test_hosted_hides_provider_key_controls():
    providers_js = (STATIC / "providers.js").read_text(encoding="utf-8")
    assert PROVIDERS_HOSTED_NOTICE in providers_js
    assert "providers-hosted-notice" in providers_js
    assert "hostedMode" in providers_js


def test_hosted_guard_label_in_ui():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert GUARD_HOSTED_LABEL in js
    assert "Guard: on \\u00b7 code checks only" in js


HOSTED_PILL_LABEL = "Check \\u00b7 code + Muse Spark (public demo)"
LOCAL_PILL_LABEL = "Local check \\u00b7 code + Muse Glimmer (30B, this PC)"


def test_hosted_header_pill_in_ui():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert HOSTED_PILL_LABEL in js
    assert LOCAL_PILL_LABEL in js
    assert re.search(r"hosted\s*\?\s*HOSTED_PILL", js)
    assert re.search(r"hostedMode\s*\?\s*HOSTED_PILL", js)
    pill_line = next(
        line for line in js.splitlines() if "HOSTED_PILL" in line and "=" in line and "Check" in line
    )
    assert "Glimmer" not in pill_line
    assert "this PC" not in pill_line
