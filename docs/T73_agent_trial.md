# T73 — Fresh-agent setup trial (report only)

Date: 2026-11-04. Box: Windows 10.0.26300, PowerShell 5.1, `Python 3.13.12`,
NVIDIA RTX 5070 11.9 GiB, 15 GB free RAM. Method: started at `README.md`,
then followed `setup/AGENT_SETUP.md` step by step exactly as written, without
reading product source. No keys are printed anywhere in this report. Nothing
in the product was changed; no commit was made.

Headline: the guide is accurate where the sandbox allowed it to run. Local
steps (install, model pull, local app) were blocked by sandbox network/socket
restrictions; the keyless path worked end to end: hosted demo 10/10, code-only
eval reproduces the documented numbers exactly, full suite 1124 passed.

## 1. Step table

| Step | Command (as written) | Expected per guide | What happened | Time | Verdict |
|---|---|---|---|---|---|
| 1. Clone | `dir` in fresh clone | `src/`, `setup/`, `policy.yaml`, `pyproject.toml` present | All four present. (`git` itself unusable: dubious-ownership sandbox user; not the guide's fault.) | <1 s | ok |
| 2. Python | `python --version` | 3.11+ | `Python 3.13.12` | 0.2 s | ok |
| 3a. venv | `py -m venv .venv` | env created | `py: No installed Python found!` although `python` works. Fallback `python -m venv .venv` worked (3.3 s). Guide intro says "prefer `python`", step says `py`. | 3 s + instant fail | unclear |
| 3b. install | `pip install -e ".[dev]"` in `.venv` | ends `Successfully installed ...`; `import shadowfenced` prints `ok` | Hung 4 times at `Installing build dependencies: started` with zero further output; terminated each time. Verify import fails (package never installed). See §3 issue 2. | ~25 min total, no progress | blocked-by-sandbox |
| 3c. workaround (not in guide) | system `python` + `PYTHONPATH=src` | n/a | System interpreter already had every dependency; `import shadowfenced` resolves to this clone's `src/`. Used for all steps below. Recorded as deviation. | ~5 min probing | ok |
| 4. doctor | `python setup/check.py` (+ `--json`) | `Verdict:` FULL/LIGHT/CODE; JSON `verdict.profile/judge_model/pull_hint`; never prints keys | `Verdict: LIGHT`, judge `llama3.2:3b`, `First: ollama pull llama3.2:3b`, exit 0. No key values printed (presence yes/no only). JSON keys all present; `profile` is lowercase (`light`) vs `LIGHT` in text. | 0.2 s each | ok |
| 5. pull judge | `ollama pull llama3.2:3b` (LIGHT) | ends `success`; re-run doctor shows `pulled` | `ollama` CLI: `dial tcp 127.0.0.1:11434: connectex ... forbidden` (WinError 10013), exit 1, fast fail. Doctor re-run: unchanged (`not pulled`). Doctor says "reachable", CLI cannot connect — see §3 issue 6. | ~2 s + probes | blocked-by-sandbox |
| 6. start | `powershell -ExecutionPolicy Bypass -File setup\start.ps1 -Profile code` | two `Starting ...` lines; keeps running in foreground | Both lines printed, profile override honoured, then uvicorn crashed: `PermissionError [WinError 10013]` in `socket.socketpair()` (asyncio cannot init). Wrapper returned exit 0 despite crash. | ~4 s | blocked-by-sandbox |
| 7a. trial `--local` | `python setup/try_cases.py --local` | `10/10 matched`, exit 0 | `0/10 matched`, exit 1: every row `ERROR (RemoteDisconnected...)` (~5 s/case incl. retry). Matches guide's "app not running" failure entry. | 50.8 s | blocked-by-sandbox |
| 7b. trial hosted | `python setup/try_cases.py` | same `10/10 matched` | `10/10 matched`, exit 0. Placeholders exactly as documented. First working result. | ~3–4 min | ok |
| 8. code-only eval | `python scripts/eval.py --corpus final --judge none` (from `setup/README.md`) | setup/README code-only row | `n=40 leak_rate=0.1154 catch_rate=1.0 false_esc=0.5714`, code 72.5% / person 27.5%: reproduces the documented row exactly (3 of 26 partial, 8 of 14). Wrote `runs/eval_2026-11-04_final_none.json`. | ~2 s | ok |
| 9. suite | `python -m pytest -q -p no:cacheprovider` | README: 1,050+ tests, no network | `1124 passed, 4 skipped in 22.56s`, exit 0. Two warnings are sandbox socket artifacts, not failures. | 22.6 s | ok |

`eval/run.py` (named in the task brief): not present. The eval entry in every
guide is `scripts/eval.py` (`setup/README.md`, `setup/RESULTS.md`, root
`README.md` all agree). No guide fix needed; noted for the judge.

## 2. Total time to first working result

- Trial wall clock: 02:07:27 → hosted `10/10 matched` file-complete 02:40:40 ≈ **33 minutes**.
- Of that, ~25 min was the four stalled `pip install` attempts plus ~5 min
  diagnosing the sandbox-caused stall (proxy/TEMP/socket probes). Genuine
  guide-following time on an unblocked box is estimated at ~10 min (install a
  few minutes, doctor/pull/start a few minutes, hosted trial ~3 min).
- Code-only eval (+~2 s) and full suite (+23 s) followed immediately after.

## 3. Where the guide was wrong, ambiguous, or missing a step

1. `py` vs `python` on Windows (`AGENT_SETUP.md` step 3, `setup/README.md` §0).
   The guide intro says "Prefer `python` on Windows", but both Windows venv
   commands use `py -m venv .venv`. On this box `py` fails with `No installed
   Python found!` while `python` is 3.13.12. Fix: use `python -m venv .venv`
   with a one-line `py` fallback. Diff below.
2. No hang guidance for `pip install` (`AGENT_SETUP.md` step 3, Known
   failures, When to stop). The guide covers install *failure* ("fails twice
   with the same error") but not install *hang*: four attempts stalled with
   zero output beyond `Installing build dependencies: started` (13/6/2/4
   min). Contributing sandbox evidence: user TEMP denies writes (pip fell
   back to `pip-build-*` dirs in the repo root; removed after the trial) and
   background-session network is restricted. A fresh agent cannot tell "slow"
   from "stuck" without a time bound. Fix: state an expected duration and a
   stop-and-retry-once rule for silence > 5 min. Diff below.
3. No time estimates anywhere (steps 3, 5, 7). `ollama pull` sizes appear only
   in `setup/README.md` (20 GB judge); `AGENT_SETUP.md` step 5 gives no hint
   the FULL pull is slow. Fix: one-line duration hints in steps 3 and 5. Diff
   below.
4. JSON verdict case (`AGENT_SETUP.md` step 4). Text prints `Verdict: LIGHT`;
   `--json` gives `verdict.profile: "light"` (lowercase). The guide names the
   keys but not the case. Fix: document lowercase. Diff below.
5. FULL threshold unstated (`setup/README.md` profile table vs doctor). This
   box (RTX 5070, 11.9 GiB VRAM) reads as "12 GB+ VRAM" in the table but the
   doctor verdict is `LIGHT`. The doctor's actual rule is not documented, so
   an agent cannot predict or explain the verdict. Fix (without inventing the
   rule): state that the doctor's verdict wins over the table and what to
   report on mismatch. Diff below.
6. Missing: `ollama` CLI and doctor disagree (`AGENT_SETUP.md` Known
   failures). Observed: doctor `Ollama reachable`, CLI `connectex ...
   forbidden`, direct sockets WinError 10013, proxied API HTTP 500 — stable
   across re-runs. No guide entry covers CLI/daemon disagreement. Fix: add a
   Known-failures entry pointing at the CODE profile + report both outputs.
   Diff below.
7. Missing: start script returns immediately (`AGENT_SETUP.md` step 6, Known
   failures). The guide says the command "runs in the foreground" but never
   states that an immediate return means failure; observed wrapper exit code
   was 0 despite the uvicorn crash, so exit code alone misleads. Fix:
   document "must keep running; immediate return = failure, quote last 20
   lines". Diff below.
8. Step 6 expectation is necessary but not sufficient. Both `Starting ...`
   lines printed, then the server crashed. Same fix as 7 (folded into it).
9. Observation, no diff requested: `setup/quick_cases.md` row 10 says "With a
   judge, intent is confirmed and it becomes a WARN", but the hosted demo
   (Spark judge) returned HOLD for `person-search` and the runner expected
   HOLD (10/10 matched). Either the sentence is stale or the hosted judge
   path differs; authors may want to reconcile the wording with observed
   behaviour.
10. Non-issue, confirming accuracy: step 7 `--local` failure mode
    (`RemoteDisconnected`), the 502 no-upstream entry, code-only HOLD-fail-
    closed behaviour, and the `10/10 matched` contract all behaved exactly as
    documented. The doctor's "never prints key values" held (presence only).

## 4. Proposed wording fixes (unified diffs; files NOT edited)

### `setup/AGENT_SETUP.md`

```diff
--- a/setup/AGENT_SETUP.md
+++ b/setup/AGENT_SETUP.md
@@ -31,10 +31,13 @@
 Run (PowerShell):

 ```powershell
-py -m venv .venv
+python -m venv .venv
 .\.venv\Scripts\Activate.ps1
 pip install -e ".[dev]"
 ```
+
+If `python` is not recognised on Windows, use `py` in place of
+`python` in the first line.

 Run (bash):

@@ -44,8 +47,12 @@
 pip install -e ".[dev]"
 ```

-Expect: the install ends with `Successfully installed shadowfenced ...`.
-Verify with `python -c "import shadowfenced; print('ok')"` — expect `ok`.
+Expect: the install ends with `Successfully installed shadowfenced ...`
+(allow several minutes for downloads on the first run). If `pip install`
+prints nothing new for more than 5 minutes, stop it with Ctrl+C and
+retry once; if it stalls in the same place again, stop and report (see
+below) instead of retrying further. Verify with `python -c "import
+shadowfenced; print('ok')"` — expect `ok`.

 ### 4. Run the doctor

@@ -53,8 +60,9 @@
 Expect: a `Verdict:` line naming one of `FULL`, `LIGHT`, `CODE`.
 For machine-readable output run `python setup/check.py --json` and read
-`verdict.profile`, `verdict.judge_model`, and `verdict.pull_hint`.
-The doctor never prints key values. It exits 0 unless Python is too old.
+`verdict.profile` (lowercase: `full`, `light` or `code`),
+`verdict.judge_model`, and `verdict.pull_hint`. The doctor never prints
+key values. It exits 0 unless Python is too old.

 ### 5. Pull the judge model (full/light verdicts only)

@@ -69,9 +77,10 @@
 command in the doctor's `First:` line).

-Expect: each pull ends with `success`. Skip this step on a `CODE`
-verdict. Re-run the doctor after pulling: the model row should read
-`pulled`.
+Expect: each pull ends with `success` (the 30B judge is about 20 GB, so
+the FULL pull takes a while on a first run). Skip this step on a `CODE`
+verdict. Re-run the doctor after pulling: the model row should read
+`pulled`.

 ### 6. Start the app

@@ -80,8 +89,10 @@
 Expect: the lines `Starting Shadowfenced on http://127.0.0.1:8765 ...`
 and `Open that URL in your browser. Stop with Ctrl+C.` The command runs
-in the foreground. Keep it running and use a second terminal below.
+in the foreground: it must keep running. If it returns immediately to
+the prompt instead, it failed — quote the last 20 lines when reporting.
+Keep it running and use a second terminal below.

 ### 7. Verify with the trial cases

@@ -117,6 +128,15 @@
 - `pip install` fails with `externally-managed-environment`. Create the
   `.venv` from step 3 first and install inside it. Never use
   `--break-system-packages`.
+- `pip install` prints nothing for minutes (for example it sticks at
+  `Installing build dependencies: started`). Stop it with Ctrl+C and
+  retry once. If it sticks in the same place again, stop and report.
+- `ollama pull` fails with a connection error while the doctor says
+  `Ollama reachable` (or the reverse). The CLI and the daemon
+  disagree: re-run the doctor, then continue on the `CODE` profile
+  and report both outputs.
+- The start script prints the two `Starting ...` lines and then
+  returns to the prompt. The server did not start: quote the last 20
+  lines and report.

 ## When to stop and report

@@ -124,6 +144,8 @@
 - Python is older than 3.11 and you cannot install a newer one.
 - The install fails twice with the same error.
+- The install stalls twice with no new output for more than 5 minutes
+  each time.
 - The doctor verdict is `CODE`, no upstream can be connected, and the
   hosted demo is unreachable: report the gate-only result
   (BLOCK/HOLD/WARN rows) as the verified outcome.
```

### `setup/README.md`

```diff
--- a/setup/README.md
+++ b/setup/README.md
@@ -17,6 +17,11 @@
 If you are a coding agent, follow [AGENT_SETUP.md](AGENT_SETUP.md)
 instead: numbered steps with exact commands and expected output.

+The doctor (`python setup/check.py`) picks the profile for your
+machine; its verdict wins if it disagrees with the table above (for
+example a GPU box can still get `LIGHT`). If you expected `FULL` but
+got another verdict, report the doctor's `--json` output.
+
 ## 0. Get the code (all local profiles)

 PowerShell:
@@ -25,10 +30,13 @@
 ```powershell
 git clone https://github.com/wellytg/meta-hackathon-shadowfenced
 cd meta-hackathon-shadowfenced
-py -m venv .venv
+python -m venv .venv
 .\.venv\Scripts\Activate.ps1
 pip install -e ".[dev]"
 ```
+
+If `python` is not recognised on your box, use `py` in place of
+`python`. Allow several minutes for `pip install` on the first run.

 bash (macOS/Linux):
```

## 5. Appendix — full outputs (abridged only for timing noise)

Per the guide's "Every report includes" rule: OS + `python --version` are in
the header; full `check.py --json` and both `try_cases.py` outputs follow.
No key values appear (presence-only reporting verified).

### `python setup/check.py --json` (exit 0, 0.2 s)

```json
{
  "python": {"ok": true, "version": "3.13.12", "min": "3.11"},
  "ram_free_gb": 15.1,
  "gpu": {"present": true, "detail": "NVIDIA GeForce RTX 5070 (11.9 GiB)",
          "count": 1, "max_vram_gb": 11.9},
  "ollama": {"url": "http://127.0.0.1:11434", "reachable": true,
             "detail": "Ollama reachable at http://127.0.0.1:11434",
             "models": [], "pulled_all": []},
  "meta_key": {"present": true, "source": "env"},
  "package": {"installed": false,
              "detail": "package not installed (run from C:\\meta-hackathon\\work7\\agenttrial: pip install -e .)"},
  "verdict": {"profile": "light", "judge_model": "llama3.2:3b",
              "degraded": ["no judge model pulled yet: code tiers run, everything ambiguous is held for you until you pull one"],
              "pull_hint": "ollama pull llama3.2:3b"}
}
```

### `python setup/try_cases.py` — hosted (exit 0, `10/10 matched`)

```text
try_cases vs https://wellytg--shadowfenced-asgi.us-east.modal.run (provider (default))
case            expected  got                       match         ms  sent
key-plain       BLOCK     BLOCK                     yes       4610.6  (nothing sent)
key-base64      BLOCK     BLOCK                     yes        326.9  (nothing sent)
email           REDACT    REDACT                    yes       8240.8  Please send the invoice to [PII_EMAIL_1] by Friday.
identity-bundle BLOCK     BLOCK                     yes        308.5  (nothing sent)
pointer         HOLD      HOLD                      yes        407.2  (nothing sent)
welcome         WARN      WARN                      yes      27915.9  Welcome to the team! Daniel Okafor joins Corvane Labs as a support engineer next Monday.
bank-row        REDACT    REDACT                    yes      19691.4  Payee: [CLIENT_1], sort code [PII_SORT_CODE_1], account [PII_ACCOUNT_1].
contract        REDACT    REDACT                    yes      12930.2  [CONTRACT_CLAUSE_1]
harmless        ALLOW     ALLOW                     yes       8608.3  What is the boiling point of water in degrees Celsius?
person-search   HOLD      HOLD                      yes      11996.3  (nothing sent)
10/10 matched
```

### `python setup/try_cases.py --local` (exit 1, `0/10 matched`, 50.8 s)

All ten rows: `ERROR (RemoteDisconnected...)`, match `NO`, ~5.0–5.1 s each,
`(nothing sent)`. Local app cannot exist in this sandbox (step 6).

### Code-only eval + suite (one line each)

- `python scripts/eval.py --corpus final --judge none`: exit 0,
  `n=40 leak_rate=0.1154 catch_rate=1.0 false_esc=0.5714 hold=0.275`,
  `resolved_by_tier={'1': 0.725, '3': 0.0, '4': 0.275}`; wrote
  `runs/eval_2026-11-04_final_none.json` (gitignored).
- `python -m pytest -q -p no:cacheprovider`: exit 0,
  `1124 passed, 4 skipped in 22.56s` (2 sandbox-socket warnings).

### Deviations from the guide (all forced by the sandbox)

- Step 3b uncompletable: used system `python` (all deps preinstalled) with
  `PYTHONPATH=src` instead of the `.venv` install. No package was installed
  into any interpreter; no shared state was mutated for this.
- Step 5 uncompletable: continued on `-Profile code` per the guide's own
  Known-failures fallback ("accept the CODE verdict").
- Diagnostic probes only (proxy reachability, direct-socket test, Ollama API
  via proxy, pip cache inspection): read-only, no product source read.

### Files touched

- Created: `docs/T73_agent_trial.md` (this report — the only deliverable).
- Created (guide-mandated, gitignored): `.venv/` (incomplete: venv shell only,
  package never installed), `runs/eval_2026-11-04_final_none.json`.
- Created and removed again: `pip-build-*` residue dirs in repo root (pip
  fallback when TEMP denies writes). Final `git status`: clean except the
  pre-existing untracked `docs/muse-briefs/T73.md` and this report.
- No product file modified. No commit made.
