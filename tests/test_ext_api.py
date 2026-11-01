"""Extension API tests (T36): scan/redact/rehydrate, pairing, origin, TTL, logs.

The ext endpoints are plain ``def`` (like the main API in
``test_api.py``), so tests call them directly with request models. The
async ``ext_guard`` never awaits, so tests drive it without an event
loop (socket creation is forbidden here). Secrets are built at runtime
from the canary registry; no secret literal appears in this file.
"""

from __future__ import annotations

import json
import logging
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from shadowfenced import redact as redact_module
from shadowfenced import ext_api
from shadowfenced.canary import CanaryRegistry
from shadowfenced.ext_api import (
    RehydrateBody,
    RedactBody,
    ScanBody,
    check_origin,
    check_token,
    ext_guard,
    ext_redact,
    ext_rehydrate,
    ext_scan,
    pairing_status,
)

SEED = "test-seed-ext"
KINDS = ("db_password", "api_key")
TOKEN = "ext-test-token-abc123"

EMAIL_TEXT = "mail ada@corvanelabs.com now"
ENTROPY_TOKEN = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"


@pytest.fixture()
def token_env(monkeypatch):
    """Pairing token comes from the environment (never from disk)."""
    monkeypatch.setenv("SHADOWFENCED_EXT_TOKEN", TOKEN)
    monkeypatch.delenv("SF_EXT_TOKEN_FILE", raising=False)
    return TOKEN


def _canary() -> str:
    return CanaryRegistry(SEED, KINDS).value("db_password")


def _req(headers: dict | None = None, method: str = "POST",
         path: str = "/api/ext/scan") -> Request:
    raw = [((k.lower()).encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "scheme": "http",
        "server": ("127.0.0.1", 8765),
        "headers": raw,
        "query_string": b"",
    }
    return Request(scope)


def _guard(request: Request) -> None:
    """Run the (non-awaiting) async guard without an event loop."""
    coro = ext_guard(request)
    try:
        coro.send(None)
    except StopIteration:
        return None
    raise AssertionError("ext_guard unexpectedly suspended")


def _auth(token: str = TOKEN, **extra) -> dict:
    headers = {"x-ext-token": token}
    headers.update(extra)
    return headers


# --- scan ---------------------------------------------------------------

def test_scan_happy_path(token_env):
    body = ext_scan(ScanBody(text=EMAIL_TEXT), None)
    assert body["count"] == 1
    (span,) = body["spans"]
    for key in ("start", "end", "class", "label", "subclass", "placeholder"):
        assert key in span
    assert span["start"] == EMAIL_TEXT.index("ada@corvanelabs.com")
    assert span["end"] == span["start"] + len("ada@corvanelabs.com")
    assert span["class"] == "pii"
    assert span["label"] == "PII_EMAIL"
    assert span["subclass"] == "email"
    assert span["placeholder"] == "[PII_EMAIL_1]"


def test_scan_clean_text(token_env):
    body = ext_scan(ScanBody(text="hello there"), None)
    assert body == {"spans": [], "count": 0}


# --- redact / rehydrate --------------------------------------------------

def test_redact_redact_and_rehydrate_round_trip(token_env):
    body = ext_redact(RedactBody(text=EMAIL_TEXT), None)
    assert body["decision"] == "REDACT"
    assert body["safe_text"] == "mail [PII_EMAIL_1] now"
    assert "ada@corvanelabs.com" not in body["safe_text"]
    assert body["id"]

    restored = ext_rehydrate(
        RehydrateBody(id=body["id"], text=body["safe_text"]), None
    )
    assert restored == {"text": EMAIL_TEXT}


def test_redact_allow_returns_text_without_id(token_env):
    body = ext_redact(RedactBody(text="hello there"), None)
    assert body["decision"] == "ALLOW"
    assert body["safe_text"] == "hello there"
    assert "id" not in body


def test_redact_block_hides_value_without_id(token_env):
    secret = _canary()
    body = ext_redact(RedactBody(text=f"password: {secret}"), None)
    assert body["decision"] == "BLOCK"
    assert body["safe_text"] == ""
    assert "id" not in body
    assert secret not in json.dumps(body)


def test_redact_hold_needs_review(token_env):
    body = ext_redact(RedactBody(text=f"maybe {ENTROPY_TOKEN}"), None)
    assert body["decision"] == "HOLD"
    assert "review" in body["notes"].lower()
    # No proven spans here, so no redacted send and no mapping id.
    assert "id" not in body


def test_rehydrate_unknown_id_404(token_env):
    with pytest.raises(HTTPException) as exc_info:
        ext_rehydrate(RehydrateBody(id="no-such-id", text="[PII_EMAIL_1]"), None)
    assert exc_info.value.status_code == 404


# --- pairing (401) ---------------------------------------------------------

def test_missing_or_wrong_token_401(token_env):
    with pytest.raises(HTTPException) as exc_info:
        check_token(_req(headers={}))
    assert exc_info.value.status_code == 401
    with pytest.raises(HTTPException) as exc_info:
        check_token(_req(headers={"x-ext-token": "wrong"}))
    assert exc_info.value.status_code == 401


def test_both_token_headers_accepted(token_env):
    check_token(_req(headers={"x-ext-token": TOKEN}))
    check_token(_req(headers={"x-pairing-token": TOKEN}))


def test_guard_requires_token_on_post(token_env):
    with pytest.raises(HTTPException) as exc_info:
        _guard(_req(headers={}, path="/api/ext/scan"))
    assert exc_info.value.status_code == 401
    _guard(_req(headers=_auth(), path="/api/ext/scan"))


# --- origin (403) ------------------------------------------------------------

@pytest.mark.parametrize("origin", [
    "https://evil.example.com",
    "http://127.0.0.1:8765",
    "https://chatgpt.com",
    "null",
])
def test_bad_origin_403(token_env, origin):
    with pytest.raises(HTTPException) as exc_info:
        check_origin(_req(headers={"origin": origin}))
    assert exc_info.value.status_code == 403


@pytest.mark.parametrize("origin", [
    "chrome-extension://abcdef",
    "CHROME-EXTENSION://ABCDEF",
    "moz-extension://01234567-89ab-cdef-0123-456789abcdef",
    "MOZ-EXTENSION://handle",
])
def test_extension_origins_pass(token_env, origin):
    check_origin(_req(headers={"origin": origin}))
    check_origin(_req(headers={"Origin": origin}))


def test_missing_origin_passes_for_loopback_console(token_env):
    check_origin(_req(headers={}))


def test_guard_checks_origin_before_token(token_env):
    # Valid token but a web origin is still rejected.
    with pytest.raises(HTTPException) as exc_info:
        _guard(_req(headers={**_auth(), "origin": "https://evil.example.com"},
                    path="/api/ext/scan"))
    assert exc_info.value.status_code == 403
    _guard(_req(headers={**_auth(), "origin": "chrome-extension://abcdef"},
                path="/api/ext/scan"))


def test_pairing_status_token_exempt_but_origin_checked(token_env):
    _guard(_req(method="GET", path="/api/ext/pairing", headers={}))
    assert pairing_status(None) == {"token": TOKEN}
    with pytest.raises(HTTPException) as exc_info:
        _guard(_req(method="GET", path="/api/ext/pairing",
                    headers={"origin": "https://evil.example.com"}))
    assert exc_info.value.status_code == 403


# --- mapping expiry (15-min TTL) ----------------------------------------------

def test_store_ttl_is_15_minutes():
    assert redact_module.STORE_TTL_S == 15 * 60


def test_mapping_expiry_returns_404(token_env):
    body = ext_redact(RedactBody(text=EMAIL_TEXT), None)
    assert body["decision"] == "REDACT"
    mapping_id = body["id"]
    # Backdate the stored entry past its TTL (monkeypatchable clock stand-in).
    mapping, _expires = redact_module._STORE[mapping_id]
    redact_module._STORE[mapping_id] = (mapping, time.time() - 1)
    assert redact_module.load_mapping(mapping_id) == {}
    with pytest.raises(HTTPException) as exc_info:
        ext_rehydrate(RehydrateBody(id=mapping_id, text=body["safe_text"]), None)
    assert exc_info.value.status_code == 404


def test_ttl_honours_monkeypatched_value(monkeypatch):
    monkeypatch.setattr(redact_module, "STORE_TTL_S", 60)
    redact_module.save_mapping("ttl-probe", {"[PII_EMAIL_1]": "a@b.c"})
    try:
        assert redact_module.load_mapping("ttl-probe") == {"[PII_EMAIL_1]": "a@b.c"}
        _, expires = redact_module._STORE["ttl-probe"]
        assert abs(expires - (time.time() + 60)) < 5
    finally:
        redact_module.drop_mapping("ttl-probe")


# --- no values in logs ----------------------------------------------------------

def test_logs_carry_no_values(token_env, caplog):
    secret = _canary()
    planted = f"password: {secret} to ada@corvanelabs.com"
    with caplog.at_level(logging.INFO, logger="shadowfenced.ext_api"):
        scan_body = ext_scan(ScanBody(text=planted), None)
        redact_body = ext_redact(RedactBody(text=planted), None)
        if redact_body.get("id"):
            ext_rehydrate(
                RehydrateBody(id=redact_body["id"],
                              text=redact_body["safe_text"]), None
            )
        with pytest.raises(HTTPException):
            ext_rehydrate(RehydrateBody(id="missing", text="x"), None)
    assert secret not in caplog.text
    assert "ada@corvanelabs.com" not in caplog.text
    assert secret not in json.dumps(scan_body)
    assert secret not in json.dumps(redact_body)
