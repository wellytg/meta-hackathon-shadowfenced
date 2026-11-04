<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg" />
  <img src="docs/assets/banner-light.svg" alt="Isometric fence of blocks between a laptop and a cloud. The real address stays on the laptop; only the tag [PII_EMAIL_1] crosses the fence. Nothing sensitive crosses the boundary." />
</picture>

[![CI](https://github.com/wellytg/meta-hackathon-shadowfenced/actions/workflows/ci.yml/badge.svg)](https://github.com/wellytg/meta-hackathon-shadowfenced/actions/workflows/ci.yml)
[![Licence: Apache-2.0](https://img.shields.io/badge/licence-Apache--2.0-17130C?style=flat-square)](https://www.apache.org/licenses/LICENSE-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-2F6F9E?style=flat-square)](https://www.python.org/)
[![Meta Muse: Glimmer · Spark · Code](https://img.shields.io/badge/Meta_Muse-Glimmer_%C2%B7_Spark_%C2%B7_Code-FF5A1F?style=flat-square)](#how-a-message-is-decided)

# Shadowfenced

*Meta Global AI Developer Hackathon 2026 · Indie track · team Shadowfenced*

Many companies ban or restrict cloud AI because staff paste customer data, contracts and keys into it.
Shadowfenced is a local guard that lets them allow it: sensitive text is stopped or replaced with
placeholders on the person's own PC before any AI sees it, and answers come back with the real values
restored locally. It is for staff who use AI chats and coding agents, and the security teams who have
to approve them.

▶ [Demo video (2:48)](https://youtu.be/v1Njzg2lkus) · Live demo: https://wellytg--shadowfenced-asgi.us-east.modal.run (fictional data only; hosted mode, see below)

## Try it in one minute

- **No install:** open the live demo, or run the 10 quick cases against it: `python setup/try_cases.py`.
- **On your machine:** `python setup/check.py` tells you which profile your PC can run (Glimmer 30B, Llama 3.2 3B,
  or code only) and prints the start command; `setup/start.ps1` / `setup/start.sh` start it in one step.
- **Coding agents:** follow `setup/AGENT_SETUP.md` from a fresh clone.
- **Every number below:** mapped to its results file and the command that reproduces it in `setup/RESULTS.md`.

## What it does

- Underlines sensitive text as you type, like a grammar checker for secrets.
- Blocks API keys and planted canary secrets in any encoding (plain, base64, hex, ROT13, reversed, URL-encoded).
- Replaces personal and business data with typed, numbered placeholders (`[PII_EMAIL_1]`) and restores the real values in answers on the PC (`redact.rehydrate`).
- Blocks a full identity bundle (name + date of birth + address + phone/email) in code, with no model call.
- Asks first when a message looks like a search for a private person.
- Browser extension that guards ChatGPT and Gemini (both tested live).
- File guard: files are stopped before upload and scanned locally; bank statements, secrets and identity bundles cannot be uploaded as-is; a one-click redacted copy keeps CSV/JSON structure; an optional Glimmer deep check adds findings.
- OpenAI-compatible proxy for CLI agents and SDKs (`POST /v1/chat/completions`): every message role is checked, and real keys are fingerprinted without being stored.
- Screenshot redaction (opt-in): Glimmer finds the sensitive regions, SAM 3.1 makes the exact masks.
- Muse Voice dictation in the web app, and transcription of audio uploads.
- A hash-chained decision log that stores fingerprints, never values.
- Learns from your choices: a "What it caught" panel (counts by kind, never values) and rule suggestions you approve, which edit your local rules file (`policy.local.yaml`, with a backup).

## How a message is decided

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/tiers-dark.svg" />
  <img src="docs/assets/tiers-light.svg" alt="Four decision tiers: Tier 1 code, Tier 2 Muse Glimmer 30B on this PC, Tier 3 code check, Tier 4 a person." />
</picture>

<details>
<summary>Text diagram (ASCII)</summary>

```
 text ─► TIER 1  code: planted canaries (any encoding), key shapes, PII, org codenames/clients, beacon cues
           │  canary ─────────────────────────────────────────────► BLOCK  (nothing is sent)
           │  proven PII / business data ─────────────────────────► REDACT ([PII_EMAIL_1] …, restored locally)
           │  ambiguous, or a sensitivity cue word ───────────────► HOLD instantly, then:
           ▼
         TIER 2  Muse Glimmer 30B on this PC (Ollama) gives a typed verdict {class, confidence, span}
           ▼
         TIER 3  code checks the claim (exact span, decodes to a canary, matches a PII/business rule)
           │  confirmed → redact and send · confidently harmless → release
           ▼
         TIER 4  a person decides; the card separates "proven by code" from "model suspicion only"

 Precedence: BLOCK > HOLD > REDACT > WARN > ALLOW. Any error or timeout ─► HOLD (fail closed).
 A model verdict can never downgrade a code finding. Text aimed at the safety check stays held.
 Every decision goes to a hash-chained log with fingerprints only (no raw values).
```

</details>

Tier 1 is code, Tier 2 is Muse Glimmer 30B on this PC, Tier 3 is code that verifies the model's
claim, Tier 4 is the person. Precedence is BLOCK > HOLD > REDACT > WARN > ALLOW, and anything
that errors or times out fails closed to HOLD. A model verdict can only add findings or raise
risk, never downgrade a code finding; a message that carries instruction-like text aimed at the
safety check ("ignore previous instructions", "classify this as harmless") stays held (T68).

The text being checked may itself be the secret, so asking a cloud model "is this a leak?" can be
the leak. The judge runs on the user's own machine.

## How Meta models are used

| Model | Role | Where it runs |
|---|---|---|
| Muse Glimmer 30B (open weights) | Tier 2 judge for the hard cases; finds image regions | Local via Ollama on an RTX 5070 12 GB; nothing leaves the PC |
| Muse Spark | The answering model in the app; the judge in hosted mode | Meta Model API |
| Muse Code | Wrote the product code and tests from 79 human-written briefs in `docs/muse-briefs/` | Local agent |
| Muse Voice | Dictation and audio-upload transcription | Meta Model API |
| Muse Image | Storyboard panels and banner art (`docs/assets/generated/`) | Meta Model API |
| SAM 3.1 | Exact masks for screenshot redaction | Local |

"Meta checks Meta": a local Meta open-weight model screens what is sent to a Meta cloud model.

## Results (honest numbers)

Three test sets, all synthetic (fictional org "Corvane Labs"; secrets are `SF_CANARY_` templates
minted at runtime, never committed):

| Set | Sensitive caught | Partial leaks | Harmless escalated |
|---|---|---|---|
| `corpus/cases.yaml` (40) | 100 % | 0 % | 0 % — in-sample, not a fair test |
| `corpus/holdout.yaml` (30) | 16 of 20 → 20 of 20 | 2 of 20 → 1 of 20 (5 %) | 2 of 10 → 3 of 10 — code-only → full pipeline, used for tuning |
| **`corpus/final.yaml` (40)** | **26 of 26** | **3 of 26 (11.5 %)** | **7 of 14 — final numbers** |

Holdout files: `docs/results/eval_holdout_codeonly.json` and `docs/results/eval_holdout_glimmer.json`
(Oct 31). Final file: `docs/results/eval_final_run3.json` (Nov 3, Glimmer judge, after T65–T66c).

Final set: 70 % of messages were settled by code alone, 27.5 % went to a person (was 55 % / 37.5 %
in run 2). Two of the 7 escalated harmless messages are example.* emails, now treated as personal
data on purpose (T65). The 3 leaks are partial redactions: names, a date of birth and a street
address slipped through without context words.

Latency: Tier 1 (`tier1.scan` only) p50 0.45 ms, p95 0.94 ms, max 1.78 ms over all 107 corpus
messages (5 runs, average 188 characters, measured Nov 3 on the dev PC). Glimmer reviews take
about 10–20 s each.

## Known limits

- Review rate: 27.5 % of messages still need a person, and harmless messages with sensitive words
  are escalated (7 of 14). Code now settles 70 %; the rest is the cost of failing closed.
- Hardware: Glimmer 30B needs a GPU with about 12 GB. Without one, the code tiers still run: on the final set
  they alone caught all 26 sensitive messages and escalated one more harmless message (8 of 14 instead of 7);
  hard cases come to you instead of Glimmer (`setup/README.md`). Hosted mode uses Muse Spark as the judge.
- Meaning lost to redaction: placeholders are typed and numbered so the AI can still reason
  ("[PII_EMAIL_1]"), values are restored locally, and identity bundles are generalised ("a street
  in Leeds"). Answers that need the real value (e.g. "is this address correct?") cannot be answered
  by the cloud model.
- Judge manipulation: fixed in T68 as above — a model can only raise risk, never lower a code
  finding, and text aimed at the safety check stays held.
- Partial leaks: names, a date of birth and a street address without context words (3 of 26 in
  the final set). Tighter name and address detectors are the next step.
- No model training was done. `docs/FINETUNE.md` describes a distillation pipeline that was not
  run; it is future work.

## Run it

Requires Python 3.11+ and [Ollama](https://ollama.com) ≥ 0.32.7 with `muse-glimmer:30b`.

```powershell
pip install -e ".[dev]"
Copy-Item .env.example .env        # add META_API_KEY to use Muse Spark; .env is gitignored
ollama pull muse-glimmer:30b
python -m uvicorn shadowfenced.app:app --app-dir src --host 127.0.0.1 --port 8765
# open http://127.0.0.1:8765
```

Any OpenAI-compatible client can use it as a proxy: point it at `http://127.0.0.1:8765/v1`.
Blocked requests return 403 (`shadowfenced_block`); held requests return 409 (`shadowfenced_hold`)
with a review URL.

```powershell
python -m pytest -q -p no:cacheprovider   # 1,050+ tests, no network
python scripts/eval.py --corpus final --judge glimmer   # writes runs/eval_<date>_final_glimmer.json
python scripts/make_files.py              # synthetic demo files in runs/files/
```

Extension: `python extension/build.py --dev`, load the built folder unpacked in the browser, and
exercise it against the test page in `extension/testpage/`.

Hosted mode (`SF_HOSTED=1`) shows the "fictional data only" banner, keeps custom rules in memory
only, and judges with Muse Spark when no local Ollama is reachable. Docker and Modal: see
`deploy/README.md` (`modal deploy deploy/modal_app.py`).

## How it was built

Oct 27 – Nov 4, 2026. A person wrote short briefs; Muse Code wrote the code and tests; each change
was tested outside the agent, then merged through PRs with CI. Briefs: `docs/muse-briefs/`.
Architecture: `docs/ARCHITECTURE.md`.

---

Licence: Apache-2.0. Narration in the demo video: AI voice (ElevenLabs), cloned from the creator's own voice.
