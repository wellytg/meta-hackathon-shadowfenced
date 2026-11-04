# Try Shadowfenced in minutes

Four ways to run it, from full local power to no install at all. Pick a
row, run the commands, then check it works with `python
setup/try_cases.py --local` (or no flag for the hosted demo). All trial
data is synthetic and fictional; never paste real secrets anywhere. The
doctor (`python setup/check.py`) never prints key values — a key's
presence is reported as yes/no only.

| Profile | Machine | What runs | What you lose |
|---|---|---|---|
| **Full** | NVIDIA GPU with 12 GB+ VRAM, Ollama, `muse-glimmer:30b` | Everything: code tiers settle clear cases, Muse Glimmer 30B judges hard cases on your GPU, answers come from Meta Muse Spark (with a key) or local Llama 3.2 3B | Nothing |
| **Light** | Any machine with Ollama and ~4 GB free RAM/VRAM | Code tiers plus a small local judge (`llama3.2:3b`, or `llama3.2:1b` on a weak box) | Weaker verdicts: more messages are held for you, and nothing gets the 30B review |
| **Code only** | No Ollama, no GPU needed | Code tiers only (canaries, key shapes, PII, policy words, beacons, re-identification) | No judge at all: every hard case is held for you (HOLD) and nothing is auto-released; without an upstream (Ollama or a Meta key) answers return 502 but gate decisions still show |
| **No install** | A browser | The hosted demo (same app, Muse Spark as the judge) | It is a public demo: fictional data only, per-IP rate limits, no key storage, your trial messages go to a hosted box |

If you are a coding agent, follow [AGENT_SETUP.md](AGENT_SETUP.md)
instead: numbered steps with exact commands and expected output.

## 0. Get the code (all local profiles)

PowerShell:

```powershell
git clone https://github.com/wellytg/meta-hackathon-shadowfenced
cd meta-hackathon-shadowfenced
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

bash (macOS/Linux):

```bash
git clone https://github.com/wellytg/meta-hackathon-shadowfenced
cd meta-hackathon-shadowfenced
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Optional: `Copy-Item .env.example .env` (PowerShell) or `cp
.env.example .env` (bash), then add `META_API_KEY` to use Meta Muse
Spark as the upstream AI. `.env` is gitignored. Without a key the
upstream is local Ollama (`llama3.2:3b`).

## Full

You need the Ollama app plus two models (about 20 GB download for the
judge):

```powershell
ollama pull muse-glimmer:30b
ollama pull llama3.2:3b
python setup/check.py
powershell -ExecutionPolicy Bypass -File setup\start.ps1 -Profile full
```

```bash
ollama pull muse-glimmer:30b
ollama pull llama3.2:3b
python3 setup/check.py
./setup/start.sh full
```

The start script sets `SF_JUDGE_MODEL=muse-glimmer:30b` and serves
<http://127.0.0.1:8765>. Open that URL: the console loads, and the
System status drawer shows the `ollama` row as `OK — Ollama reachable;
muse-glimmer:30b listed`.

## Light

Any machine with Ollama and ~4 GB free memory. Meta's `llama3.2:3b`
acts as the judge via `SF_JUDGE_MODEL=llama3.2:3b` (the start script
sets it). On a very weak box use `llama3.2:1b` instead — set
`SF_JUDGE_MODEL=llama3.2:1b` before starting:

```powershell
ollama pull llama3.2:3b
python setup/check.py
powershell -ExecutionPolicy Bypass -File setup\start.ps1 -Profile light
```

```bash
ollama pull llama3.2:3b
python3 setup/check.py
./setup/start.sh light
```

Open <http://127.0.0.1:8765>. The `ollama` status row reads `OK —
Ollama reachable; llama3.2:3b listed`. Expect more HOLDs than on full:
the small judge is cautious, and borderline cases wait for you.

## Code only

No Ollama, no models, no GPU. The doctor's verdict is `CODE` and the
app still starts:

```powershell
python setup/check.py
powershell -ExecutionPolicy Bypass -File setup\start.ps1 -Profile code
```

```bash
python3 setup/check.py
./setup/start.sh code
```

Open <http://127.0.0.1:8765>. Code tiers still run — clear secrets
still BLOCK, clear PII still redacts — but hard cases fail closed
(HOLD) with nobody to review them. The `ollama` status row reads `Down
— Ollama unreachable: ConnectError`, and when Ollama is up but the
judge model is not pulled it reads `Degraded — Ollama reachable;
<model> is not listed`. Without any upstream (no Ollama, no Meta key),
messages that would be sent return `502 shadowfenced_upstream`, but
every gate decision (BLOCK/REDACT/HOLD/WARN) still shows correctly.

## No install

Open the hosted demo: <https://wellytg--shadowfenced-asgi.us-east.modal.run>
(fictional data only). It shows the banner `Public demo. Use made-up
data only.` and the `ollama` status row reads `Off — Not used in the
public demo — Muse Spark does the Tier 2 check`. No account, no keys:
paste the cases from [quick_cases.md](quick_cases.md) into the console
and watch the decisions.

## Check it works

Against your local app (run in a second terminal while the app runs):

```powershell
python setup/try_cases.py --local
```

```bash
python3 setup/try_cases.py --local
```

Against the hosted demo (default) or any URL:

```powershell
python setup/try_cases.py
python setup/try_cases.py --url http://192.168.1.5:8765
```

You should see `10/10 matched`, one row per case with the decision and
what was sent. The hosted demo can be slow to wake: the runner waits up
to 90 s per request and retries once. See
[quick_cases.md](quick_cases.md) for what each case proves, and
[RESULTS.md](RESULTS.md) for where every claimed number comes from.

## Providers (destination models)

The console's Providers page lists Meta Muse Spark, OpenAI, Gemini,
Anthropic, OpenRouter, and local Ollama. Only **Meta and Ollama were
tested live**; the rest are OpenAI-compatible entries nobody has tried
end to end yet. Keys are stored in your OS credential store (or `.env`
as a fallback) and the app only ever reports their presence as yes/no.
`--provider <id>` on `try_cases.py` sends through that provider when it
is connected.

## Measured on the final test set

Per-profile scores on `corpus/final.yaml` (40 cases: 26 sensitive, 14 harmless). Raw files in `docs/results/`.

| Profile | Judge | Settled by code | To a person | Leaks | Harmless held | Command |
|---|---|---|---|---|---|---|
| Full | muse-glimmer:30b | 70 % | 27.5 % | 3 of 26 partial | 7 of 14 | `python scripts/eval.py --corpus final --judge glimmer` |
| Light | llama3.2:3b | not yet measured | | | | `python scripts/eval.py --corpus final --judge llama` |
| Code only | none | 72.5 % | 27.5 % | 3 of 26 partial | 8 of 14 | `python scripts/eval.py --corpus final --judge none` |

All 26 sensitive messages were caught in both measured profiles. On this set, running without a GPU costs one more harmless message held for you; Glimmer's value is in cases code cannot prove (pointers to secrets, clearing harmless messages with sensitive words). Files: `eval_final_run3.json` (Full), `eval_final_codeonly.json` (Code only).
| No install | Muse Spark (hosted) | not measured | | | | (hosted demo; see T72) |
