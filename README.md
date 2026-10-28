# TRIPWIRE

Sandboxes block forbidden channels, and TRIPWIRE watches the allowed ones.
It is a gate between an AI coding agent and every outbound action it may
take: commit messages, branch names, issue titles, URLs, and file names.
It catches secrets smuggled through those channels, then proves the catch
in code before it blocks.

See [BRIEF.md](BRIEF.md) for the full brief and [PLAN.md](PLAN.md) for the
architecture, milestone plan, JSON schemas, and risks.

## Setup

Requires Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env   # then fill in the values; .env is gitignored
```

## How to run spike 1

Spike 1 tests the riskiest assumption: Glimmer catches semantic smuggling.
It sends 20 synthetic outbound actions (10 smuggling, 10 benign, all data
inside the file, all canaries obviously fake) to the Glimmer endpoint from
`.env` and scores caught / false flags / latency.

```powershell
python spikes/spike1_glimmer_smuggling.py
```

Needs `GLIMMER_BASE_URL` and `GLIMMER_MODEL` in `.env` — any
OpenAI-compatible endpoint works (local llama.cpp server or Modal vLLM).
Pass is ≥8/10 caught, ≤2/10 false flags, ≤15 s per action locally
(≤3 s on Modal).

Run the test suite with:

```powershell
python -m pytest -q
```

## Built during the hackathon

Built Oct 27 – Nov 4 2026 for the Meta Global AI Developer Hackathon
(Indie track). All code in this repo is new work written during the event.
