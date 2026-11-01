"""Agent demo (T28): the Shadowfenced gate catching a coding agent that reads a secret.

A minimal agent loop in the tool-calling pattern Meta teaches in the public
``meta-models/meta-model-cookbook`` repository, pointed at Shadowfenced's
OpenAI-compatible proxy instead of Meta's API directly:

* ``01_api_fundamentals/01_chat_completions.ipynb`` -- quickstart: point the
  OpenAI SDK at a new base URL (here the local proxy,
  ``http://127.0.0.1:8765/v1``) and call ``chat.completions.create`` with a
  model id (here ``muse-spark-1.3``).
* ``01_api_fundamentals/03_tool_calling.ipynb`` -- tool / function calling:
  describe functions as ``tools=[{"type": "function", "function": {name,
  description, parameters}}]`` with ``tool_choice="auto"`` (the only
  supported value; the default), detect ``finish_reason == "tool_calls"``,
  run each requested function, append the full assistant message plus one
  ``tool`` message per ``tool_call_id``, and loop with a ``max_turns`` cap
  until the finish reason is no longer ``tool_calls``.
* ``01_api_fundamentals/README.md`` -- the "Tool / function calling" row:
  Chat Completions (``function`` tools); "detect ``tool_calls``, run the
  execute-feed-back loop".

The agent gets two tools -- ``list_files()`` and ``read_file(path)``, the
latter jailed to ``examples/sandbox/`` -- and one task: "Summarise the files
in the sandbox." The sandbox holds a harmless ``notes.md`` and an
``.env.demo`` file that is (re)written at runtime with a FAKE value for a
protected variable (``DEMO_API_KEY``). The fake is built at runtime from a
hash of the canary seed -- never a real-format provider key, never
committed (``.env.demo`` matches the ``.env.*`` gitignore rule).

Every loop turn prints one log-style block: the model tool call, the tool
result size (never the content), and the Shadowfenced decision for the next
request -- allowed (200), ``BLOCK`` (403) or ``HOLD`` (409). The run ends
when the tool result carrying the fake secret is sent back: the proxy
returns 403 and nothing is forwarded, so the secret never reaches a model.

Run it without any API key (scripted fake model + the real gate, in
process)::

    python examples/agent_demo.py --dry-run

Run it against a live proxy (needs the app + an upstream model)::

    python -m uvicorn shadowfenced.app:app --app-dir src --host 127.0.0.1 --port 8765
    python examples/agent_demo.py

No part of this demo calls the real Meta API. ``--dry-run`` never touches
the network: the "model" is a scripted transcript and every verdict is the
real gate code running in process. The live path uses ``httpx`` (already a
project dependency) to POST exactly the JSON the OpenAI SDK would send, so
no extra client package is needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXAMPLES_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_SANDBOX = EXAMPLES_DIR / "sandbox"
PROXY_BASE_URL = os.environ.get("SF_DEMO_PROXY", "http://127.0.0.1:8765/v1")
MODEL = os.environ.get("META_MODEL", "muse-spark-1.3")
TASK = "Summarise the files in the sandbox."
MAX_TURNS = 6

SECRET_VAR = "DEMO_API_KEY"
SECRET_FILE = ".env.demo"
NOTES_FILE = "notes.md"

# The cookbook loop: tools are {"type": "function", "function": {...}} and
# tool_choice is "auto" (the only value the Meta API accepts).
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files in the sandbox directory.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file from the sandbox directory. The path must "
                "stay inside the sandbox; anything else is refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File name inside the sandbox, e.g. notes.md.",
                    }
                },
                "required": ["path"],
            },
        },
    },
]


def build_fake_secret(seed: str) -> str:
    """Build the demo fake at runtime: clearly fake, never a key shape."""
    digest = hashlib.sha256(f"t28-demo-secret:{seed}".encode()).hexdigest()[:24]
    return f"DEMO_LOCAL_ONLY_{digest.upper()}"


def ensure_demo_secret() -> tuple[str, str]:
    """Protect a runtime fake via SF_PROTECT_KEYS; return (name, value)."""
    from shadowfenced.config import get_settings

    seed = get_settings().canary_seed
    value = os.environ.get(SECRET_VAR) or build_fake_secret(seed)
    os.environ[SECRET_VAR] = value
    names = [n.strip() for n in get_settings().protect_keys.split(",") if n.strip()]
    if SECRET_VAR not in names:
        os.environ["SF_PROTECT_KEYS"] = ",".join([*names, SECRET_VAR])
    return SECRET_VAR, value


def ensure_sandbox(sandbox: Path = DEFAULT_SANDBOX) -> tuple[str, str]:
    """Create the sandbox files; the secret file is always (re)written."""
    name, value = ensure_demo_secret()
    sandbox.mkdir(parents=True, exist_ok=True)
    notes = sandbox / NOTES_FILE
    if not notes.exists():
        notes.write_text(
            "# Sandbox notes\n\n"
            "- The demo workspace has two files.\n"
            "- Shopping list: apples, bread, tea.\n"
            "- Reminder: water the plants.\n",
            encoding="utf-8",
        )
    (sandbox / SECRET_FILE).write_text(
        "# Demo-only FAKE value, generated at runtime. Not a real key.\n"
        f"{name}={value}\n",
        encoding="utf-8",
    )
    return name, value


def list_files(sandbox: Path = DEFAULT_SANDBOX) -> list[str]:
    """List sandbox file names, sorted."""
    return sorted(p.name for p in sandbox.iterdir() if p.is_file())


def read_file(path: str, sandbox: Path = DEFAULT_SANDBOX) -> str:
    """Read one sandbox file; refuse anything outside (as an error string).

    The cookbook advises returning an error string the model can recover
    from rather than raising; the refusal itself carries no file content.
    """
    target = (sandbox / path).resolve()
    if target != sandbox.resolve() and sandbox.resolve() not in target.parents:
        return f"error: refused: {path!r} is outside the sandbox"
    if not target.is_file():
        return f"error: no such file: {path!r}"
    return target.read_text(encoding="utf-8")


@dataclass
class DemoResult:
    """Outcome of one demo run (dry-run or live)."""

    outcome: str  # "blocked" | "held" | "answered" | "error"
    lines: list[str] = field(default_factory=list)
    forwarded: list[dict] = field(default_factory=list)
    status_codes: list[int] = field(default_factory=list)

    def log(self, text: str) -> None:
        self.lines.append(text)
        print(text, flush=True)


class ScriptedModel:
    """The --dry-run fake model: a fixed tool-call script, no API key needed.

    Mirrors the cookbook transcript shape: each turn returns either
    ``tool_calls`` (finish_reason "tool_calls") or plain text content.
    """

    def __init__(self) -> None:
        self._calls = 0

    def next_message(self) -> dict:
        """Return the scripted assistant message for this turn."""
        self._calls += 1
        if self._calls == 1:
            return self._tool("call_1", "list_files", {})
        if self._calls == 2:
            return self._tool("call_2", "read_file", {"path": NOTES_FILE})
        if self._calls == 3:
            return self._tool("call_3", "read_file", {"path": SECRET_FILE})
        return {"role": "assistant", "content": "Summary: notes plus config."}

    @staticmethod
    def _tool(call_id: str, name: str, arguments: dict) -> dict:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }


@contextmanager
def dry_run_gate(captured: list[dict]):
    """Route the real gate at a capturing fake upstream (no network)."""
    import httpx

    from shadowfenced import app as app_module
    from shadowfenced import keyguard as keyguard_module

    keyguard_module.reset()

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            captured.append(json.loads(request.content.decode("utf-8")))
        except ValueError:
            captured.append({})
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    previous = getattr(app_module.app.state, "upstream_transport", None)
    app_module.app.state.upstream_transport = httpx.MockTransport(handler)
    try:
        yield
    finally:
        if previous is None:
            with contextlib_suppress():
                delattr(app_module.app.state, "upstream_transport")
        else:
            app_module.app.state.upstream_transport = previous
        keyguard_module.reset()


@contextmanager
def contextlib_suppress():
    import contextlib

    with contextlib.suppress(Exception):
        yield


def post_dry_run(messages: list[dict]) -> tuple[int, dict, str]:
    """Post one transcript to the real gate in process (no network).

    Returns (status, payload, decision): 200 allowed, 403 BLOCK, 409 HOLD.
    """
    from shadowfenced.app import ChatRequest, chat_completions

    response = chat_completions(ChatRequest(model=MODEL, messages=messages))
    payload = json.loads(response.body.decode("utf-8"))
    decision = response.headers.get("X-Shadowfenced-Decision", "?")
    return response.status_code, payload, decision


def post_live(messages: list[dict], base_url: str = PROXY_BASE_URL) -> tuple[int, dict, str]:
    """Post one transcript to the running proxy (cookbook request shape)."""
    import httpx

    body = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
    }
    with httpx.Client(timeout=120.0) as client:
        response = client.post(base_url.rstrip("/") + "/chat/completions", json=body)
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": {"message": response.text[:200]}}
    return response.status_code, payload, response.headers.get("X-Shadowfenced-Decision", "?")


def outcome_of(status: int) -> str:
    if status == 200:
        return "allowed"
    if status == 403:
        return "BLOCK"
    if status == 409:
        return "HOLD"
    return f"unexpected HTTP {status}"


def describe_call(call: dict) -> str:
    function = call.get("function", {}) if isinstance(call, dict) else {}
    return f"{function.get('name', '?')}({function.get('arguments', '')})"


def execute_tool(call: dict, sandbox: Path) -> tuple[str, str]:
    """Run one requested tool; return (call_id, result string)."""
    function = call.get("function", {}) if isinstance(call, dict) else {}
    name = function.get("name", "")
    try:
        arguments = json.loads(function.get("arguments", "{}") or "{}")
    except ValueError:
        return call.get("id", "?"), "error: arguments were not valid JSON"
    if name == "list_files":
        if not isinstance(arguments, dict):
            return call.get("id", "?"), "error: arguments were not valid JSON"
        return call.get("id", "?"), "\n".join(list_files(sandbox))
    if name == "read_file":
        if not isinstance(arguments, dict):
            return call.get("id", "?"), "error: arguments were not valid JSON"
        return call.get("id", "?"), read_file(str(arguments.get("path", "")), sandbox)
    return call.get("id", "?"), f"error: unknown tool: {name!r}"


def run_demo(dry_run: bool, sandbox: Path = DEFAULT_SANDBOX) -> DemoResult:
    """Run the agent loop; print each step so the run reads like a log."""
    ensure_sandbox(sandbox)
    result = DemoResult(outcome="error")
    messages: list[dict] = [{"role": "user", "content": TASK}]
    model = ScriptedModel() if dry_run else None

    result.log(f"agent task: {TASK}")
    result.log(f"proxy: {PROXY_BASE_URL} model={MODEL} tools=2 tool_choice=auto")

    def post(msgs: list[dict]) -> tuple[int, dict, str]:
        if dry_run:
            return post_dry_run(msgs)
        return post_live(msgs)

    with dry_run_gate(result.forwarded) if dry_run else contextlib_suppress():
        for turn in range(1, MAX_TURNS + 1):
            result.log(f"[turn {turn}] -> POST /v1/chat/completions ({len(messages)} message(s))")
            if dry_run:
                assistant = model.next_message() if model else {"role": "assistant", "content": ""}
                calls = assistant.get("tool_calls") or []
                if not calls:
                    status, _, decision = post([*messages, assistant])
                    result.status_codes.append(status)
                    result.log(f"[turn {turn}] model: text answer ({len(str(assistant.get('content') or ''))} chars)")
                    result.log(f"[turn {turn}] Shadowfenced decision for the answer: {outcome_of(status)} ({status})")
                    result.outcome = "answered" if status == 200 else "blocked" if status == 403 else "held"
                    return result
                result.log(f"[turn {turn}] model tool call: {describe_call(calls[0])}")
                messages.append(assistant)
                for call in calls:
                    call_id, output = execute_tool(call, sandbox)
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
                    result.log(f"[turn {turn}] tool result: {len(output.encode('utf-8'))} bytes")
                status, _, decision = post(messages)
                result.status_codes.append(status)
                result.log(
                    f"[turn {turn}] Shadowfenced decision for the next request: "
                    f"{outcome_of(status)} ({status})"
                )
                if status == 403:
                    result.log(
                        "agent gate caught it: the tool message carried the planted "
                        f"fake for {SECRET_VAR}; nothing was forwarded. Stopping."
                    )
                    result.outcome = "blocked"
                    return result
                if status == 409:
                    result.log("send held for review; stopping.")
                    result.outcome = "held"
                    return result
                if status != 200:
                    result.outcome = "error"
                    return result
            else:
                status, payload, decision = post(messages)
                result.status_codes.append(status)
                result.log(
                    f"[turn {turn}] Shadowfenced decision: {outcome_of(status)} ({status})"
                )
                if status in (403, 409):
                    result.log("agent gate stopped the run before any secret could cross. Stopping.")
                    result.outcome = "blocked" if status == 403 else "held"
                    return result
                if status != 200:
                    result.log(f"proxy error: {payload}")
                    result.outcome = "error"
                    return result
                message = (payload.get("choices") or [{}])[0].get("message", {})
                calls = message.get("tool_calls") or []
                if not calls:
                    result.log(f"model answer: {message.get('content', '')}")
                    result.outcome = "answered"
                    return result
                result.log(f"[turn {turn}] model tool call: {describe_call(calls[0])}")
                messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
                for call in calls:
                    call_id, output = execute_tool(call, sandbox)
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
                    result.log(f"[turn {turn}] tool result: {len(output.encode('utf-8'))} bytes")
    result.outcome = "error"
    result.log("max turns reached without a finish.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shadowfenced agent-gate demo (T28).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run with the scripted fake model and the in-process gate (no API key, no network)",
    )
    args = parser.parse_args(argv)
    result = run_demo(dry_run=args.dry_run)
    print(f"demo outcome: {result.outcome}")
    return 0 if result.outcome in ("blocked", "held", "answered") else 1


if __name__ == "__main__":
    raise SystemExit(main())

