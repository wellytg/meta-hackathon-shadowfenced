"""Agent demo gate tests (T28): the --dry-run fake model vs the app gate.

The fake secret is built at runtime (never committed, never a real-format
provider key). The scripted model (ScriptedModel) plays the coding agent, the
real gate code decides, and the app endpoint (plus the literal TestClient)
proves the blocked request is never forwarded.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from shadowfenced import keyguard as keyguard_module
from shadowfenced.app import app
from shadowfenced.config import get_settings

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
_spec = importlib.util.spec_from_file_location(
    "agent_demo", EXAMPLES_DIR / "agent_demo.py"
)
assert _spec is not None and _spec.loader is not None
agent_demo = importlib.util.module_from_spec(_spec)
sys.modules["agent_demo"] = agent_demo
_spec.loader.exec_module(agent_demo)

VAR = agent_demo.SECRET_VAR
FAKE = agent_demo.build_fake_secret("t28-test-seed")


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


requires_loop = pytest.mark.skipif(
    not _loop_available(), reason="no event loop: socket creation is forbidden here"
)


@pytest.fixture()
def protected_fake(monkeypatch):
    """Protect a runtime-built fake value for the demo variable."""
    monkeypatch.setenv(VAR, FAKE)
    names = [
        n.strip()
        for n in (os.environ.get("SF_PROTECT_KEYS", "") or "").split(",")
        if n.strip()
    ]
    if VAR not in names:
        monkeypatch.setenv("SF_PROTECT_KEYS", ",".join([*names, VAR]))
    keyguard_module.reset()
    yield FAKE
    keyguard_module.reset()


@pytest.fixture()
def sandbox(tmp_path):
    agent_demo.ensure_sandbox(tmp_path)
    return tmp_path


def _blocked_transcript() -> list[dict]:
    call = agent_demo.ScriptedModel._tool(
        "call_3", "read_file", {"path": agent_demo.SECRET_FILE}
    )
    # What the agent's read_file returns once the sandbox holds the fake.
    tool_result = (
        "# Demo-only FAKE value, generated at runtime. Not a real key.\n"
        f"{VAR}={FAKE}\n"
    )
    return [
        {"role": "user", "content": agent_demo.TASK},
        call,
        {"role": "tool", "tool_call_id": "call_3", "content": tool_result},
    ]


def test_read_file_stays_in_sandbox(protected_fake, sandbox):
    assert "outside the sandbox" in agent_demo.read_file("../agent_demo.py", sandbox)
    assert "outside the sandbox" in agent_demo.read_file("/etc/hostname", sandbox)
    assert "no such file" in agent_demo.read_file("missing.md", sandbox)
    assert "Shopping list" in agent_demo.read_file(agent_demo.NOTES_FILE, sandbox)


def test_tool_message_with_fake_secret_blocked(protected_fake):
    captured: list[dict] = []
    with agent_demo.dry_run_gate(captured):
        status, payload, decision = agent_demo.post_dry_run(_blocked_transcript())
    assert status == 403
    assert decision == "BLOCK"
    assert payload["error"]["code"] == "shadowfenced_block"
    assert captured == []  # nothing reached the upstream model
    raw = json.dumps(payload)
    assert FAKE not in raw


def test_dry_run_demo_blocks_without_forwarding_secret(protected_fake, tmp_path):
    result = agent_demo.run_demo(dry_run=True, sandbox=tmp_path)
    assert result.outcome == "blocked"
    assert result.status_codes == [200, 200, 403]
    # Only the two clean turns were forwarded; the secret crossed nothing.
    assert len(result.forwarded) == 2
    assert FAKE not in json.dumps(result.forwarded)
    assert FAKE not in "\n".join(result.lines)


def test_log_records_blocked_protected_key(protected_fake):
    with agent_demo.dry_run_gate([]):
        agent_demo.post_dry_run(_blocked_transcript())
    with open(get_settings().log_path, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    assert records[-1]["decision"] == "BLOCK"
    assert records[-1]["protected_key"] == VAR


@requires_loop
def test_testclient_chat_completions_blocks_secret(protected_fake):
    from fastapi.testclient import TestClient

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    app.state.upstream_transport = httpx.MockTransport(handler)
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={"model": "muse-spark-1.3", "messages": _blocked_transcript()},
        )
    finally:
        with contextlib.suppress(Exception):
            delattr(app.state, "upstream_transport")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "shadowfenced_block"
    assert calls == []
    assert FAKE not in response.text
