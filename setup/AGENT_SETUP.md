# Agent setup: from fresh clone to verified app

Follow these steps in order from a fresh clone. Use PowerShell on
Windows, bash on macOS/Linux. Prefer `python` on Windows and `python3`
on macOS/Linux; the steps below write `python` — swap in `python3`
where your box needs it.

## Steps

### 1. Clone the repo

Run:

```powershell
git clone https://github.com/wellytg/meta-hackathon-shadowfenced
cd meta-hackathon-shadowfenced
```

Expect: a directory with `src/`, `setup/`, `policy.yaml`,
`pyproject.toml`. Verify with `dir` (PowerShell) or `ls` (bash).

### 2. Check the Python version

Run `python --version`.

Expect: `Python 3.11` or newer (for example `Python 3.13.12`).
Stop and report if it is older than 3.11: nothing below runs.

### 3. Create the project environment and install

Run (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

If `python` is not recognised on Windows, use `py` in place of
`python` in the first line.

Run (bash):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Expect: the install ends with `Successfully installed shadowfenced ...`
(allow several minutes for downloads on the first run). While it runs,
`pip` prints progress lines — new output means it is still working. If
`pip install` prints nothing new for more than 5 minutes, stop it with
Ctrl+C and retry once; if it stalls in the same place again, stop and
report (see below) instead of retrying further. Verify with `python -c
"import shadowfenced; print('ok')"` — expect `ok`.

### 4. Run the doctor

Run `python setup/check.py`.

Expect: a `Verdict:` line naming one of `FULL`, `LIGHT`, `CODE`.
For machine-readable output run `python setup/check.py --json` and read
`verdict.profile` (lowercase: `full`, `light` or `code`),
`verdict.judge_model` (`muse-glimmer:30b`, `llama3.2:3b`,
`llama3.2:1b`, or `""` when there is no judge), and `verdict.pull_hint`
(`""`, or `ollama pull llama3.2:3b` when Ollama is up but no judge
model is pulled). The doctor never prints key values. It exits 0
unless Python is too old.

The verdict rule is: `FULL` means Ollama is reachable and
`muse-glimmer:30b` is pulled; `LIGHT` means Ollama is reachable (with
or without a small judge pulled); `CODE` means Ollama is unreachable.
GPU memory is advisory only — below 11 GiB of VRAM the doctor keeps
`FULL` and warns the 30B judge may answer slowly. The doctor's verdict
wins over any profile table: if you expected a different profile,
report the doctor's `--json` output.

### 5. Pull the judge model (full/light verdicts only)

If the verdict is `FULL`, run:

```powershell
ollama pull muse-glimmer:30b
ollama pull llama3.2:3b
```

If the verdict is `LIGHT`, run `ollama pull llama3.2:3b` (or the exact
command in the doctor's `First:` line).

Expect: each pull ends with `success` (the 30B judge is about 20 GB, so
the FULL pull takes a while on a first run — tens of minutes on a slow
link; the small `llama3.2:3b` model is a few GB and takes a few
minutes). While a pull runs, `ollama pull` prints per-layer progress
bars — moving percentages mean it is still working. If it prints
nothing new for more than 5 minutes, stop it with Ctrl+C and retry once
(a pull resumes where it stopped). Skip this step on a `CODE`
verdict. Re-run the doctor after pulling: the model row should read
`pulled`.

### 6. Start the app

Run (PowerShell) `powershell -ExecutionPolicy Bypass -File
setup\start.ps1` or (bash) `./setup/start.sh`. Add `-Profile
full|light|code` (PowerShell) or `full|light|code` (bash) to override
the doctor's recommendation.

Expect: the lines `Starting Shadowfenced on http://127.0.0.1:8765 ...`
and `Open that URL in your browser. Stop with Ctrl+C.` The command runs
in the foreground: it must keep running. If it returns immediately to
the prompt instead, it failed — quote the last 20 lines when reporting.
Keep it running and use a second terminal below.

### 7. Verify with the trial cases

Run `python setup/try_cases.py --local` in a second terminal.

Expect: a 10-row table ending with `10/10 matched`, and exit 0.
Each row shows case, expected decision, got, match, latency, and what
was sent. If the box cannot run a local app, run `python
setup/try_cases.py` instead (hosted demo) and expect the same `10/10
matched`.

## Known failures

- `ollama: command not found`. Install Ollama from https://ollama.com,
  or accept the `CODE` verdict and continue without a judge.
- Doctor says `Ollama unreachable`. Start the Ollama app/service and
  re-run the doctor. If Ollama cannot run here, use the `CODE` profile.
- Doctor says a model is `not pulled`. Run the `First:` pull command,
  then re-run the doctor.
- `python -m uvicorn ...` fails with `No module named uvicorn`. The
  install in step 3 did not finish. Re-run it inside the activated
  `.venv`.
- `try_cases.py` shows `ERROR` with `ConnectionRefused` /
  `RemoteDisconnected`. The app from step 6 is not running. Start it
  and retry.
- `try_cases.py` shows `ERROR` with `502` on REDACT/ALLOW rows only.
  No upstream is connected (no Ollama, no Meta key). BLOCK/HOLD/WARN
  rows still validate the gate. Connect Ollama or set `META_API_KEY`,
  then retry — or verify against the hosted demo instead.
- Hosted demo is slow or returns 5xx once. The runner already retries
  once with a 90 s timeout. Wait a minute and retry once more before
  reporting.
- `pip install` fails with `externally-managed-environment`. Create the
  `.venv` from step 3 first and install inside it. Never use
  `--break-system-packages`.
- `pip install` prints nothing for minutes (for example it sticks at
  `Installing build dependencies: started`). Stop it with Ctrl+C and
  retry once. If it sticks in the same place again, stop and report.
- `ollama pull` fails with a connection error while the doctor says
  `Ollama reachable` (or the reverse). The CLI and the daemon
  disagree: re-run the doctor, then continue on the `CODE` profile
  and report both outputs.
- The start script prints the two `Starting ...` lines and then
  returns to the prompt. The server did not start: quote the last 20
  lines and report.

## When to stop and report

Stop and report instead of retrying further when any of these holds:

- Python is older than 3.11 and you cannot install a newer one.
- The install fails twice with the same error.
- The install stalls twice with no new output for more than 5 minutes
  each time.
- The doctor verdict is `CODE`, no upstream can be connected, and the
  hosted demo is unreachable: report the gate-only result
  (BLOCK/HOLD/WARN rows) as the verified outcome.
- `try_cases.py` mismatches on the same case twice after a clean
  restart of the app: quote the full row plus the doctor `--json`.

Every report includes: the OS and `python --version`, the full
`python setup/check.py --json` output, the full `python
setup/try_cases.py [--local]` output, and what you already tried from
the known-failures list.
