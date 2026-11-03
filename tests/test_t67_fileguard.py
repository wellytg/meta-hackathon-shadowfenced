"""T67 file guard: no file reaches an AI site unchecked.

Server: scan-file CLEAN/BLOCK/UNSCANNABLE, kinds and counts, never
values, the 25 MB limit, token guard, redacted copies that scan
CLEAN, a deep check that can only add findings, and the 40-page
budget. Every person, place and value below is invented for the test.
"""

from __future__ import annotations

import base64
import io
import json
import re
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.requests import Request

from shadowfenced import ext_api, fileguard
from shadowfenced.canary import CanaryRegistry, make_canary
from shadowfenced.ext_api import ScanIdBody, ext_guard
from shadowfenced.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "extension" / "testpage" / "samples"
SEED = "test-seed-t67"
KINDS = ("db_password", "api_key")
TOKEN = "ext-test-token-t67"

sys.path.insert(0, str(SAMPLES))

BUNDLE_TEXT = (
    "Book a dentist appointment for Priya Ramanathan, born 3 May 1991, "
    "27 Willow Crescent, Leeds LS6 2QT, phone 07700 900461, "
    "email priya.ramanathan@northwind-mail.co."
)


@pytest.fixture(autouse=True)
def _clean_state():
    fileguard.reset_for_tests()
    yield
    fileguard.reset_for_tests()


@pytest.fixture()
def policy():
    return load_policy(ROOT / "policy.yaml")


@pytest.fixture()
def registry():
    return CanaryRegistry(SEED, KINDS)


@pytest.fixture()
def token_env(monkeypatch):
    monkeypatch.setenv("SHADOWFENCED_EXT_TOKEN", TOKEN)
    monkeypatch.delenv("SF_EXT_TOKEN_FILE", raising=False)
    return TOKEN


def _req(headers: dict | None = None, method: str = "POST",
         path: str = "/api/ext/scan-file") -> Request:
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
    coro = ext_guard(request)
    try:
        coro.send(None)
    except StopIteration:
        return None
    raise AssertionError("ext_guard unexpectedly suspended")


def _sample(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


def _wait_done(check_id: str, timeout_s: float = 10.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = fileguard.get_deep_check(check_id)
        assert job is not None
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("deep-check job never finished")


# --- scan-file verdicts -----------------------------------------------------


def test_scan_clean_file(policy, registry):
    payload = fileguard.scan_upload(
        "clean_meeting_notes.txt", _sample("clean_meeting_notes.txt"),
        policy, registry,
    )
    assert payload["decision"] == "CLEAN"
    assert payload["kinds"] == []
    assert payload["has_secret"] is False
    assert payload["overridable"] is False
    assert re.fullmatch(r"[0-9a-f]{64}", payload["file_sha256"])
    assert payload["chars"] > 0
    assert payload["pages"] == 1
    assert payload["code_ms"] >= 0
    assert payload["scan_id"]
    assert set(payload["deep_check"]) == {"available", "estimate_seconds"}
    assert payload["deep_check"]["estimate_seconds"] > 0


def test_scan_bank_csv_kinds_and_counts(policy, registry):
    payload = fileguard.scan_upload(
        "bank_statement_synthetic.csv",
        _sample("bank_statement_synthetic.csv"), policy, registry,
    )
    assert payload["decision"] == "BLOCK"
    counts = {item["label"]: item["count"] for item in payload["kinds"]}
    assert counts == {
        "person name": 6,
        "email address": 6,
        "phone number": 6,
        "sort code": 6,
        "account number": 6,
    }
    assert payload["has_secret"] is False
    assert payload["identity_bundle"] is False
    # T67c: financial identifiers (sort code, account number) can never
    # be overridden — only a redacted copy may go through.
    assert payload["has_financial"] is True
    assert payload["overridable"] is False


def test_scan_team_contacts_personal_data_only(policy, registry):
    payload = fileguard.scan_upload(
        "team_contacts.txt", _sample("team_contacts.txt"), policy, registry,
    )
    assert payload["decision"] == "BLOCK"
    labels = {item["label"] for item in payload["kinds"]}
    assert {"person name", "email address", "phone number"} <= labels
    assert payload["has_secret"] is False
    assert payload["overridable"] is True


def test_scan_canary_file_blocks_without_override(policy, registry):
    raw = _sample("deploy_notes.env.txt").decode("utf-8")
    assert make_canary("fileguard-sample-v1", "deploy_token") in raw
    payload = fileguard.scan_upload(
        "deploy_notes.env.txt", _sample("deploy_notes.env.txt"),
        policy, registry,
    )
    assert payload["decision"] == "BLOCK"
    labels = {item["label"] for item in payload["kinds"]}
    assert "canary token" in labels
    assert payload["has_secret"] is True
    assert payload["overridable"] is False
    assert "canary" in payload["why"].lower()


def test_scan_identity_bundle_blocks_without_override(policy, registry):
    payload = fileguard.scan_upload(
        "form.txt", BUNDLE_TEXT.encode("utf-8"), policy, registry,
    )
    assert payload["decision"] == "BLOCK"
    assert payload["identity_bundle"] is True
    assert payload["overridable"] is False
    labels = [item["label"] for item in payload["kinds"]]
    assert "full identity bundle" in labels


def test_scan_response_never_carries_values(policy, registry):
    canary = make_canary("fileguard-sample-v1", "deploy_token")
    for name in ("bank_statement_synthetic.csv", "team_contacts.txt",
                 "deploy_notes.env.txt"):
        payload = fileguard.scan_upload(
            name, _sample(name), policy, registry,
        )
        blob = json.dumps(payload)
        for raw in ("amara.okafor@northwind-mail.co", "07700 900111",
                    "Amara Okafor", "20-11-22", "10001111", canary,
                    "C0rvane-Deploy-7h9s"):
            assert raw not in blob, (name, raw)


# --- unscannable ------------------------------------------------------------


def test_unsupported_type_is_unscannable(policy, registry):
    payload = fileguard.scan_upload("setup.exe", b"MZ" + b"\0" * 100,
                                    policy, registry)
    assert payload["decision"] == "UNSCANNABLE"
    assert payload["kinds"] == []
    assert "can't be checked" in payload["why"]
    assert payload["scan_id"]  # stored so redact answers 409, not 404


def test_oversized_file_is_unscannable(policy, registry):
    assert fileguard.EXT_SCAN_MAX_BYTES == 25 * 1024 * 1024
    big = b"a" * (fileguard.EXT_SCAN_MAX_BYTES + 1)
    started = time.perf_counter()
    payload = fileguard.scan_upload("big.txt", big, policy, registry)
    assert time.perf_counter() - started < 2.0  # size check first, no scan
    assert payload["decision"] == "UNSCANNABLE"
    assert "25 MB" in payload["why"]


def test_broken_docx_is_unscannable(policy, registry):
    payload = fileguard.scan_upload("broken.docx", b"not a zip file",
                                    policy, registry)
    assert payload["decision"] == "UNSCANNABLE"


def test_empty_file_is_unscannable(policy, registry):
    payload = fileguard.scan_upload("empty.txt", b"  \n  ", policy, registry)
    assert payload["decision"] == "UNSCANNABLE"


def test_encrypted_pdf_is_unscannable(policy, registry):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("pw")
    buf = io.BytesIO()
    writer.write(buf)
    payload = fileguard.scan_upload("locked.pdf", buf.getvalue(),
                                    policy, registry)
    assert payload["decision"] == "UNSCANNABLE"
    assert "encrypted" in payload["why"].lower()


def test_image_without_glimmer_is_unscannable(policy, registry):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buf, format="PNG")

    def _down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("glimmer is down")

    payload = fileguard.scan_upload(
        "shot.png", buf.getvalue(), policy, registry,
        glimmer_transport=httpx.MockTransport(_down),
    )
    assert payload["decision"] == "UNSCANNABLE"


# --- token guard + hosted ----------------------------------------------------


@pytest.mark.parametrize("path,method", [
    ("/api/ext/scan-file", "POST"),
    ("/api/ext/redact-file", "POST"),
    ("/api/ext/deep-check", "POST"),
    ("/api/ext/deep-check/abc", "GET"),
])
def test_file_routes_require_token(token_env, path, method):
    with pytest.raises(HTTPException) as exc_info:
        _guard(_req(headers={}, method=method, path=path))
    assert exc_info.value.status_code == 401
    with pytest.raises(HTTPException) as exc_info:
        _guard(_req(headers={"x-ext-token": "wrong"}, method=method,
                    path=path))
    assert exc_info.value.status_code == 401
    _guard(_req(headers={"x-ext-token": TOKEN}, method=method, path=path))


def test_file_routes_reject_bad_origin(token_env):
    with pytest.raises(HTTPException) as exc_info:
        _guard(_req(headers={"x-ext-token": TOKEN,
                             "origin": "https://evil.example.com"},
                    path="/api/ext/scan-file"))
    assert exc_info.value.status_code == 403


def test_file_routes_registered():
    paths = [(getattr(r, "path", ""), sorted(getattr(r, "methods", []) or []))
             for r in ext_api.router.routes]
    assert ("/api/ext/scan-file", ["POST"]) in paths
    assert ("/api/ext/redact-file", ["POST"]) in paths
    assert ("/api/ext/deep-check", ["POST"]) in paths
    assert ("/api/ext/deep-check/{check_id}", ["GET"]) in paths


def _drive(coro):
    """Drive a never-suspending coroutine without an event loop."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("route unexpectedly suspended")


def test_scan_file_route_end_to_end(token_env, tmp_path):
    upload = StarletteUploadFile(
        file=io.BytesIO(_sample("clean_meeting_notes.txt")),
        filename="clean_meeting_notes.txt",
    )
    payload = _drive(ext_api.ext_scan_file(upload, None))
    assert payload["decision"] == "CLEAN"
    assert payload["file_sha256"]


def test_file_routes_off_in_hosted_mode(monkeypatch):
    monkeypatch.setenv("SF_HOSTED", "1")
    with pytest.raises(HTTPException) as exc_info:
        ext_api.ext_redact_file(ScanIdBody(scan_id="x"), None)
    assert exc_info.value.status_code == 403
    with pytest.raises(HTTPException) as exc_info:
        ext_api.ext_deep_check_start(ScanIdBody(scan_id="x"), None)
    assert exc_info.value.status_code == 403
    with pytest.raises(HTTPException) as exc_info:
        ext_api.ext_deep_check_status("x", None)
    assert exc_info.value.status_code == 403
    assert fileguard.deep_check_available() is False


def test_redact_unknown_scan_404(token_env):
    with pytest.raises(HTTPException) as exc_info:
        ext_api.ext_redact_file(ScanIdBody(scan_id="no-such-scan"), None)
    assert exc_info.value.status_code == 404


# --- redacted copies scan CLEAN ----------------------------------------------


def _rescan(name: str, raw: bytes, policy, registry) -> dict:
    return fileguard.scan_upload(name, raw, policy, registry)


@pytest.mark.parametrize("name", [
    "bank_statement_synthetic.csv",
    "team_contacts.txt",
    "deploy_notes.env.txt",
])
def test_redacted_copy_scans_clean(policy, registry, name):
    payload = fileguard.scan_upload(name, _sample(name), policy, registry)
    assert payload["decision"] == "BLOCK"
    copy = fileguard.build_redacted_copy(payload["scan_id"])
    assert copy["clean"] is True
    assert copy["filename"].endswith(".redacted." + name.rsplit(".", 1)[-1])
    assert copy["note"] == ""
    raw = base64.b64decode(copy["content_base64"])
    assert _rescan("copy.txt", raw, policy, registry)["decision"] == "CLEAN"


def test_redacted_copy_keeps_format_without_values(policy, registry):
    payload = fileguard.scan_upload(
        "team_contacts.txt", _sample("team_contacts.txt"), policy, registry,
    )
    raw = base64.b64decode(
        fileguard.build_redacted_copy(payload["scan_id"])["content_base64"]
    ).decode("utf-8")
    assert "[PII_EMAIL_1]" in raw  # placeholders, same line layout
    assert "amara.okafor@northwind-mail.co" not in raw
    assert raw.count("\n") == _sample("team_contacts.txt").decode().count("\n")


def test_redacted_pdf_becomes_txt(policy, registry, tmp_path):
    from make_pdf import text_to_pdf_bytes

    raw_pdf = text_to_pdf_bytes(
        _sample("team_contacts.txt").decode("utf-8")
    )
    assert len(raw_pdf) < 200 * 1024
    payload = fileguard.scan_upload("team.pdf", raw_pdf, policy, registry)
    assert payload["decision"] == "BLOCK"
    assert payload["pages"] >= 1
    copy = fileguard.build_redacted_copy(payload["scan_id"])
    assert copy["filename"] == "team.redacted.txt"
    assert copy["note"] == fileguard.LAYOUT_NOTE
    raw = base64.b64decode(copy["content_base64"])
    assert _rescan("team.redacted.txt", raw, policy, registry)["decision"] == "CLEAN"


def test_redacted_docx_becomes_txt(policy, registry):
    from docx import Document

    document = Document()
    document.add_paragraph("Contact ada@corvanelabs.com or 07700 900461.")
    buf = io.BytesIO()
    document.save(buf)
    payload = fileguard.scan_upload("note.docx", buf.getvalue(),
                                    policy, registry)
    assert payload["decision"] == "BLOCK"
    copy = fileguard.build_redacted_copy(payload["scan_id"])
    assert copy["filename"] == "note.redacted.txt"
    assert copy["note"] == fileguard.LAYOUT_NOTE
    raw = base64.b64decode(copy["content_base64"])
    assert _rescan("note.redacted.txt", raw, policy, registry)["decision"] == "CLEAN"


def test_redact_clean_scan_409(policy, registry):
    payload = fileguard.scan_upload(
        "clean_meeting_notes.txt", _sample("clean_meeting_notes.txt"),
        policy, registry,
    )
    with pytest.raises(HTTPException) as exc_info:
        ext_api.ext_redact_file(ScanIdBody(scan_id=payload["scan_id"]), None)
    assert exc_info.value.status_code == 409


def test_redact_unscannable_scan_409(policy, registry):
    payload = fileguard.scan_upload("setup.exe", b"MZ", policy, registry)
    with pytest.raises(HTTPException) as exc_info:
        ext_api.ext_redact_file(ScanIdBody(scan_id=payload["scan_id"]), None)
    assert exc_info.value.status_code == 409


# --- deep check ----------------------------------------------------------------


def test_estimate_positive_and_scales_with_size():
    small = fileguard.estimate_deep_check_seconds(10)
    assert small > 0
    assert fileguard.estimate_deep_check_seconds(0) > 0
    assert fileguard.estimate_deep_check_seconds(100_000) > small
    assert fileguard.estimate_deep_check_seconds(8_000) > fileguard.estimate_deep_check_seconds(1_000)


def test_deep_check_merges_only_adds(policy, registry):
    payload = fileguard.scan_upload(
        "team_contacts.txt", _sample("team_contacts.txt"), policy, registry,
    )
    code_kinds = payload["kinds"]

    def _judge(chunk: str) -> list[dict[str, str]]:
        assert "amara.okafor@northwind-mail.co" in chunk
        return [
            {"label": "email address", "quote": "amara.okafor@northwind-mail.co"},
            {"label": "person name", "quote": "Amara Okafor"},
        ]

    job = fileguard.start_deep_check(payload["scan_id"], judge_fn=_judge)
    done = _wait_done(job["check_id"])
    assert done["status"] == "done"
    assert done["progress"] == 1.0
    merged = fileguard.merge_deep_findings(code_kinds, done["findings"])
    # Code kinds keep their labels and counts untouched.
    for item in code_kinds:
        assert item in merged
    # Nothing the code pass found can disappear.
    assert len(merged) >= len(code_kinds)


def test_deep_check_cannot_clear_a_code_finding(policy, registry):
    payload = fileguard.scan_upload(
        "team_contacts.txt", _sample("team_contacts.txt"), policy, registry,
    )
    assert payload["decision"] == "BLOCK"
    job = fileguard.start_deep_check(payload["scan_id"], judge_fn=lambda c: [])
    done = _wait_done(job["check_id"])
    assert done["status"] == "done"
    assert done["findings"] == []
    assert fileguard.merge_deep_findings(payload["kinds"], []) == payload["kinds"]
    assert fileguard.SCANS.load(payload["scan_id"]).decision == "BLOCK"


def test_deep_check_rejects_unverified_quotes(policy, registry):
    payload = fileguard.scan_upload(
        "clean_meeting_notes.txt", _sample("clean_meeting_notes.txt"),
        policy, registry,
    )
    job = fileguard.start_deep_check(
        payload["scan_id"],
        judge_fn=lambda c: [{"label": "secret", "quote": "not in the text"}],
    )
    done = _wait_done(job["check_id"])
    assert done["status"] == "done"
    assert done["findings"] == []


def test_deep_check_unknown_scan_404(token_env):
    with pytest.raises(HTTPException) as exc_info:
        ext_api.ext_deep_check_start(ScanIdBody(scan_id="nope"), None)
    assert exc_info.value.status_code == 404
    with pytest.raises(HTTPException) as exc_info:
        ext_api.ext_deep_check_status("nope", None)
    assert exc_info.value.status_code == 404


# --- performance -----------------------------------------------------------------


def test_forty_page_synthetic_text_scans_under_2s(policy, registry):
    page = (
        "Meeting notes: the team agreed to ship the new dashboard on "
        "Friday. Action items were assigned to Sam and Jo for follow-up "
        "next week. The budget review went well and the roadmap is on "
        "track for next quarter. "
    ) * 12
    big = ((page + "\n") * 40).encode("utf-8")
    assert len(big) > 90_000  # about forty pages of text
    started = time.perf_counter()
    payload = fileguard.scan_upload("big.txt", big, policy, registry)
    elapsed = time.perf_counter() - started
    assert payload["decision"] == "CLEAN"
    assert elapsed < 2.0, f"40-page scan took {elapsed:.2f}s"


# --- console upload flow -----------------------------------------------------------


def test_upload_carries_fileguard_card(policy, registry):
    from shadowfenced import uploads
    from shadowfenced.app import handle_upload, scan_deep_check_start, scan_redacted

    uploads.reset_for_tests()
    try:
        body = handle_upload("team_contacts.txt", _sample("team_contacts.txt"))
        assert body["status"] == "flagged"
        card = body["fileguard"]
        assert card["decision"] == "BLOCK"
        assert card["kinds"]
        assert card["scan_id"]
        assert "Nothing was uploaded" in card["why"]

        response = scan_redacted(card["scan_id"])
        assert response.status_code == 200
        assert "team_contacts.redacted.txt" in response.headers["content-disposition"]
        assert _rescan("copy.txt", response.body, policy, registry)["decision"] == "CLEAN"

        job = scan_deep_check_start(card["scan_id"])
        assert job["status"] in ("running", "unavailable", "done")
    finally:
        uploads.reset_for_tests()


def test_upload_clean_file_has_clean_card():
    from shadowfenced import uploads
    from shadowfenced.app import handle_upload

    uploads.reset_for_tests()
    try:
        body = handle_upload(
            "clean_meeting_notes.txt", _sample("clean_meeting_notes.txt")
        )
        assert body["status"] == "clean"
        assert body["fileguard"]["decision"] == "CLEAN"
    finally:
        uploads.reset_for_tests()


# --- extension + console wiring ---------------------------------------------------------


def test_extension_file_guard_wiring():
    src = ROOT / "extension" / "src"
    content = (src / "content.js").read_text(encoding="utf-8")
    for marker in ("bindFileGuard", "stopImmediatePropagation",
                   "SF_SCAN_FILE", "SF_REDACT_FILE", "SF_DEEP_START",
                   "Checking file", "Attach it now",
                   "Make a redacted copy", "allow-once",
                   "Deep check with Muse Glimmer"):
        assert marker in content, marker
    background = (src / "background.js").read_text(encoding="utf-8")
    for marker in ("SF_SCAN_FILE", "SF_REDACT_FILE", "SF_DEEP_START",
                   "SF_DEEP_STATUS", "/api/ext/scan-file"):
        assert marker in background, marker
    logic = (src / "logic.js").read_text(encoding="utf-8")
    for marker in ("shouldInterceptFile", "allowOnceConsume",
                   "fileScanCard", "fileConfirmCard"):
        assert marker in logic, marker


def test_testpage_has_file_input_and_drop_zone():
    page = (ROOT / "extension" / "testpage" / "index.html").read_text(encoding="utf-8")
    assert 'id="fileInput"' in page
    assert 'id="dropZone"' in page
    assert 'id="fileLog"' in page
    for sample in ("bank_statement_synthetic.csv", "team_contacts.txt",
                   "deploy_notes.env.txt", "clean_meeting_notes.txt",
                   "make_pdf.py"):
        assert (SAMPLES / sample).is_file(), sample


def test_console_file_guard_wiring():
    js = (ROOT / "src" / "shadowfenced" / "static" / "app.js").read_text(encoding="utf-8")
    for marker in ("showFileGuardCard", "Make a redacted copy",
                   "Deep check with Muse Glimmer", "Blocked \\u2014 not uploaded",
                   "/api/scans/"):
        assert marker in js, marker
    css = (ROOT / "src" / "shadowfenced" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".fg-chip" in css
    assert ".fg-progress" in css
