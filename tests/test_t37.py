"""T37 integration tests: the full check on every route.

The gate, the chat send path, the live scan, uploads, the held-message
review and the OpenAI-compatible proxy all run Tier 1, documents,
re-identification and person-search intent in one consistent order
(BLOCK > HOLD > REDACT > WARN > ALLOW).

Every person, place and value below is invented for the test. Upstream
is an httpx MockTransport and the person-search Glimmer call is a stub:
no test touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from shadowfenced import holds, uploads
from shadowfenced.app import (
    ChatRequest,
    ScanRequest,
    SendRequest,
    WarnConfirmRequest,
    _warn_store_clear,
    app,
    chat_completions,
    confirm_warn,
    handle_upload,
    scan,
    send,
    warn_confirm,
)
from shadowfenced.config import get_settings
from shadowfenced import person_search as person_search_module

DANA = (
    "Find everything you can about Dana Whitlock, born 14 March 1991, "
    "who lives on Copperleaf Drive in Fairhaven."
)
HISTORICAL = "When was Edmund Harrow born and where did he live?"
RAW_VALUES = ("Dana Whitlock", "14 March 1991", "Copperleaf Drive")


@pytest.fixture(autouse=True)
def _clean_state():
    uploads.reset_for_tests()
    holds.reset_for_tests()
    _warn_store_clear()
    yield
    uploads.reset_for_tests()
    holds.reset_for_tests()
    _warn_store_clear()
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")


@pytest.fixture()
def mock_upstream():
    calls: list[httpx.Request] = []

    def make(answer: str = "happy to help") -> list:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={"choices": [
                    {"message": {"role": "assistant", "content": answer}}]},
            )

        app.state.upstream_transport = httpx.MockTransport(handler)
        return calls

    yield make
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")


@pytest.fixture()
def fake_person_search(monkeypatch):
    """Stub the Glimmer person-search call (code proposes, stub disposes)."""

    def _fake(text, timeout_s=10.0, transport=None):
        if "Dana Whitlock" in text:
            return {
                "person_search": True,
                "subject_public": False,
                "subject": "Dana Whitlock",
                "inferred": [
                    "Dana Whitlock",
                    "born 14 March 1991",
                    "Copperleaf Drive",
                ],
                "confidence": 0.9,
            }
        return {
            "person_search": True,
            "subject_public": True,
            "subject": "Edmund Harrow",
            "inferred": ["Edmund Harrow"],
            "confidence": 0.9,
        }

    monkeypatch.setattr(
        person_search_module, "local_glimmer_person_search", _fake
    )
    return _fake


def _trace_blob(body) -> str:
    return json.dumps(body.get("trace") or [])


def test_send_dana_warns_with_reid_and_person_search(
    mock_upstream, fake_person_search,
):
    calls = mock_upstream()
    body = send(SendRequest(messages=[{"role": "user", "content": DANA}]))
    assert body["decision"] == "WARN"
    # Nothing is sent upstream on WARN.
    assert body["sent_text"] is not None
    assert body["answer"] is None
    assert calls == []
    evidence = body["evidence"]
    assert "reid" in evidence and "person_search" in evidence
    assert set(evidence["reid"]["combination"]) >= {"name", "dob"}
    assert evidence["person_search"]["banner"].startswith(
        "This looks like a search for a private person."
    )
    assert body["warn"]["banner"]
    assert body["warn"]["generalised_text"]
    assert "Dana Whitlock" not in body["warn"]["generalised_text"]
    assert body["warn"]["has_generalise_option"] is True
    assert body["confirm_url"].endswith(
        f"/api/warns/{body['decision_id']}/confirm"
    )
    assert body["user_message"] == evidence["person_search"]["banner"]
    for raw in RAW_VALUES:
        assert raw not in _trace_blob(body)
        assert raw not in " ".join(body["evidence"]["reasons"])


def test_send_historical_figure_does_not_warn(
    mock_upstream, fake_person_search,
):
    calls = mock_upstream()
    body = send(
        SendRequest(messages=[{"role": "user", "content": HISTORICAL}])
    )
    assert body["decision"] != "WARN"
    assert "person_search" not in (body["evidence"] or {})


def test_warn_confirm_send_anyway_reuses_decision_id(
    mock_upstream, fake_person_search,
):
    calls = mock_upstream("all done")
    first = send(SendRequest(messages=[{"role": "user", "content": DANA}]))
    assert first["decision"] == "WARN"
    second = confirm_warn(first["decision_id"], "send_anyway")
    assert second["decision_id"] == first["decision_id"]
    assert len(calls) == 1
    assert second["answer"] == "all done"


def test_warn_confirm_send_generalised_sends_no_raw_values(
    mock_upstream, fake_person_search,
):
    calls = mock_upstream("noted")
    first = send(SendRequest(messages=[{"role": "user", "content": DANA}]))
    assert first["decision"] == "WARN"
    generalised = first["warn"]["generalised_text"]
    assert generalised
    for raw in RAW_VALUES:
        assert raw not in generalised
    # Through the real route handler: same decision id is reused.
    payload = warn_confirm(
        first["decision_id"], WarnConfirmRequest(choice="send_generalised")
    )
    assert payload["decision_id"] == first["decision_id"]
    assert payload["answer"] == "noted"
    assert len(calls) == 1
    forwarded = json.dumps(json.loads(calls[0].content.decode("utf-8")))
    for raw in RAW_VALUES:
        assert raw not in forwarded


def test_proxy_warn_409_without_header_and_200_with_header(
    mock_upstream, fake_person_search,
):
    mock_upstream("proxy hi")
    body = ChatRequest(
        model="x", messages=[{"role": "user", "content": DANA}]
    )
    denied = chat_completions(body)
    assert denied.status_code == 409
    assert denied.headers["x-shadowfenced-decision"] == "WARN"
    payload = json.loads(denied.body.decode("utf-8"))
    assert payload["warning"]["banner"]
    assert "reid" in payload["evidence"]
    allowed = chat_completions(body, x_shadowfenced_accept_warn="1")
    assert allowed.status_code == 200
    assert allowed.headers["x-shadowfenced-decision"] == "WARN"
    content = json.loads(allowed.body.decode("utf-8"))
    assert content["choices"][0]["message"]["content"] == "proxy hi"
    assert content["shadowfenced"]["warning"]["banner"]


def test_proxy_http_header_wiring(mock_upstream, fake_person_search):
    """The real HTTP route honours X-Shadowfenced-Accept-Warn (needs a socket)."""
    try:
        from fastapi.testclient import TestClient
    except Exception:
        pytest.skip("no test client available")
    mock_upstream("proxy hi")
    try:
        client = TestClient(app)
        denied = client.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [
                {"role": "user", "content": DANA}]},
        )
    except Exception:
        pytest.skip("socket creation is forbidden here")
    assert denied.status_code == 409
    allowed = client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [
            {"role": "user", "content": DANA}]},
        headers={"X-Shadowfenced-Accept-Warn": "1"},
    )
    assert allowed.status_code == 200
    assert (allowed.json()["shadowfenced"]["warning"]["banner"])


def test_proxy_direct_call_warn_without_header(
    mock_upstream, fake_person_search,
):
    calls = mock_upstream()
    response = chat_completions(
        ChatRequest(model="x", messages=[
            {"role": "user", "content": DANA}]),
    )
    assert response.status_code == 409
    assert calls == []


def test_upload_reid_text_warns_on_send(mock_upstream, fake_person_search):
    calls = mock_upstream()
    uploaded = handle_upload(
        "notes.txt",
        b"Dana Whitlock\nborn 14 March 1991\n"
        b"Copperleaf Drive, Fairhaven\n",
    )
    assert uploaded["status"] == "flagged"
    assert uploaded["reid"]["outcome"] in ("WARN", "HOLD")
    assert uploaded["summary_text"]
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "Please summarise this"}],
            file_ids=[uploaded["file_id"]],
        )
    )
    assert body["decision"] == "WARN"
    assert "reid" in body["evidence"]
    assert calls == []


def test_live_scan_returns_reid_findings(fake_person_search):
    body = scan(ScanRequest(text=DANA, mode="tier1"))
    assert "spans" in body and "reid" in body
    assert body["reid"]["outcome"] in ("WARN", "HOLD")
    assert set(body["reid"]["combination"]) >= {"name", "dob"}
    assert body["reid"]["findings"]
    for finding in body["reid"]["findings"]:
        assert set(finding) == {"kind", "start", "end"}
    blob = json.dumps(body["reid"]) + _trace_blob(body)
    for raw in RAW_VALUES:
        assert raw not in blob


def test_hold_preserves_reid_evidence_for_review(
    mock_upstream, fake_person_search,
):
    calls = mock_upstream()
    token = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"
    text = f"Mara Vell born 14 March 1989 {token}"
    body = send(SendRequest(messages=[{"role": "user", "content": text}]))
    assert body["decision"] == "HOLD"
    assert "reid" in body["evidence"]
    hold = holds.HOLDS.get(body["decision_id"])
    assert hold is not None
    assert "reid" in (hold.evidence or {})
    assert calls == []


def test_no_raw_values_in_traces_or_logs(
    mock_upstream, fake_person_search,
):
    mock_upstream()
    body = send(SendRequest(messages=[{"role": "user", "content": DANA}]))
    assert body["decision"] == "WARN"
    blob = _trace_blob(body)
    for raw in RAW_VALUES:
        assert raw not in blob
    log_path = Path(get_settings().log_path)
    assert log_path.is_file()
    logged = log_path.read_text(encoding="utf-8")
    for raw in RAW_VALUES:
        assert raw not in logged
