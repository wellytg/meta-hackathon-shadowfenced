# Shadowfenced

Shadowfenced is a local layer between any person and any AI. It underlines
sensitive text as you type (like a grammar checker), checks uploaded files
the same way, and lets only clean or redacted text cross the organisation's
boundary.

## Setup

Requires Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env   # then fill in the values; .env is gitignored
```

Run the test suite with:

```powershell
python -m pytest -q
```

## Built during the hackathon

Built Oct 27 – Nov 4 2026 for the Meta Global AI Developer Hackathon
(Indie track). All code in this repo is new work written during the event.
