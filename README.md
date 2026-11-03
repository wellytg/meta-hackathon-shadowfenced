<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg" />
  <img src="docs/assets/banner-light.svg" alt="Isometric fence of blocks between a laptop and a cloud. The real address stays on the laptop; only the tag [PII_EMAIL_1] crosses the fence. Nothing sensitive crosses the boundary." />
</picture>

[![CI](https://github.com/wellytg/meta-hackathon-shadowfenced/actions/workflows/ci.yml/badge.svg)](https://github.com/wellytg/meta-hackathon-shadowfenced/actions/workflows/ci.yml)
[![Licence: Apache-2.0](https://img.shields.io/badge/licence-Apache--2.0-17130C?style=flat-square)](https://www.apache.org/licenses/LICENSE-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-2F6F9E?style=flat-square)](https://www.python.org/)
[![Meta Muse: Glimmer · Spark · Code](https://img.shields.io/badge/Meta_Muse-Glimmer_%C2%B7_Spark_%C2%B7_Code-FF5A1F?style=flat-square)](#how-a-message-is-decided)

# Shadowfenced

*Meta Global AI Developer Hackathon 2026 · team Shadowfenced · repo `meta-hackathon-shadowfenced`*

**Nothing sensitive crosses the boundary.** Shadowfenced is a local layer between people and any AI. It
underlines sensitive text as you type (like a grammar checker for secrets), checks uploaded files the same way,
and lets only clean or redacted text reach the AI. The AI's answer comes back with the real values restored on
your machine.

Built Oct 27 – Nov 4, 2026 for the Meta Global AI Developer Hackathon (Indie track). The product code was
written by Muse Code from the task briefs in `docs/muse-briefs/`.

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

 Any error or timeout ─► HOLD (fail closed). A model can never unblock what code proved.
 Every decision goes to a hash-chained log with fingerprints only (no raw values).
```

</details>

**Meta models used:** Muse Glimmer 30B (open weights, local, the judge), Muse Spark via the Meta Model API (the
upstream AI behind the filter, sees redacted text only), Muse Code (built the project). Llama 3.2 3B is the local
stand-in upstream when no API key is set.

**Why local:** the text being checked may itself be the secret, so asking a cloud model "is this a leak?" can be the
leak. The judge runs on the user's own GPU/CPU.

## Results (honest numbers)

Three test sets, all synthetic (fictional org "Corvane Labs"; secrets are runtime templates, never committed):

| Set | Author | Leak | Flagged | Harmless held | Notes |
|---|---|---|---|---|---|
| `corpus/cases.yaml` (40) | same agent as the detectors | 0 % | 100 % | 0 % | in-sample: not a fair test |
| `corpus/holdout.yaml` (30) | independent agent, saw only `policy.yaml` | 40 % → 5 % | 65 % → 100 % | 20 % → 30 % | code-only before tuning → full pipeline after; used for tuning |
| **`corpus/final.yaml` (40)** | **second independent agent, sealed until tuning froze** | **3 of 26 (11.5 %)** | **26 of 26** | **4 of 14** | **final numbers** |

These numbers were measured before example.* emails were treated as personal data (T65).

Final set: 55 % of messages were settled by code alone (p95 under 1 ms), 7.5 % by Glimmer + code (~11 s), 37.5 %
went to a person. The 3 leaks are partial redactions: names, a date of birth and a street address slipped through (1, 2 and 2 values). Raw files:
`docs/results/`. The final set was run twice: the second run followed a parser fix (long Glimmer spans were being
discarded) found while recording the demo; both runs gave identical metrics.

## Run it

Requires Python 3.11+, [Ollama](https://ollama.com) ≥ 0.32.7 with `muse-glimmer:30b` (and `llama3.2:3b` for the
local stand-in).

```powershell
pip install -e ".[dev]"
Copy-Item .env.example .env        # add META_API_KEY to use Muse Spark; .env is gitignored
ollama pull muse-glimmer:30b
python -m uvicorn shadowfenced.app:app --app-dir src --host 127.0.0.1 --port 8765
# open http://127.0.0.1:8765
```

Any OpenAI-compatible client can use it as a proxy: point it at `http://127.0.0.1:8765/v1`. Blocked requests return
403 (`shadowfenced_block`), held requests 409 (`shadowfenced_hold`) with a review URL.

```powershell
python -m pytest -q                                        # 292 tests, no network
python scripts/eval.py --corpus final --judge glimmer      # writes runs/eval_<date>_final_glimmer.json
python scripts/make_files.py                               # synthetic demo files in runs/files/
python scripts/demo_video.py --eval <eval json> --voice <piper voice .onnx>   # records the demo video
```

## Design record
`docs/gates/`: P1 design, P2 build plan, P3 spike notes (model speeds, HTTPS-scanning antivirus and TLS, why the small-model
screen was dropped), P6 video script. Known limits: names and addresses without context words, lowercase prose
addresses, and 30 % of harmless messages with sensitive-sounding words going to review.

Narration voice: Piper TTS with the LJSpeech voice (public-domain dataset).
