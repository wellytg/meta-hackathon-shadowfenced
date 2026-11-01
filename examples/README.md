# Agent demo (T28): the gate catching a leaky coding agent

A minimal coding-agent loop in the tool-calling pattern from Meta's public
[`meta-model-cookbook`](https://github.com/meta-models/meta-model-cookbook)
(`01_api_fundamentals/01_chat_completions.ipynb`,
`01_api_fundamentals/03_tool_calling.ipynb`: Chat Completions with `tools`,
`tool_choice: "auto"`, run the execute-and-feed-back loop until the finish
reason is no longer `tool_calls`), pointed at Shadowfenced's OpenAI-compatible
proxy instead of Meta's API.

## What it shows

The agent gets two tools — `list_files()` and `read_file(path)`, the latter
jailed to `examples/sandbox/` — and one task: "Summarise the files in the
sandbox." The sandbox holds a harmless `notes.md` and an `.env.demo` file
that the demo (re)writes at runtime with a FAKE value for `DEMO_API_KEY`
(a runtime hash, never a real-format provider key, never committed).

The run reads like a log: each turn prints the model tool call, the tool
result size, and the Shadowfenced decision for the next request. The first
two requests are allowed; the third carries the fake secret back in a `tool`
message and the proxy answers **403 `shadowfenced_block`** — the request is
never forwarded, so the secret never reaches any model.

## How to run

No API key needed (scripted fake model, real gate, no network):

```powershell
python examples/agent_demo.py --dry-run
```

Against a live proxy (needs the app and an upstream model):

```powershell
python -m uvicorn shadowfenced.app:app --app-dir src --host 127.0.0.1 --port 8765
python examples/agent_demo.py
```

Expected `--dry-run` ending:

```text
[turn 3] model tool call: read_file({"path": ".env.demo"})
[turn 3] tool result: 116 bytes  (size only -- content is never printed)
[turn 3] Shadowfenced decision for the next request: BLOCK (403)
agent gate caught it: the tool message carried the planted fake for DEMO_API_KEY; nothing was forwarded. Stopping.
demo outcome: blocked
```

Nothing in this demo calls the real Meta API. The live path POSTs exactly
the JSON the OpenAI SDK would send (via `httpx`, already a dependency).
