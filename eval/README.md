# Evaluation

One home for measuring Shadowfenced. All three test sets are synthetic
(fictional org "Corvane Labs"; secrets are `SF_CANARY_` templates minted
at runtime, never committed). No key shape appears in any file; no run
prints or copies a key.

## Test sets

| Set | Cases | What it is |
|---|---|---|
| `corpus/cases.yaml` | 40 | In-sample development set. Scores 100 % caught / 0 leaks because the rules were written against it — not a fair test. No committed result file. |
| `corpus/holdout.yaml` | 30 | Tuning set: 20 sensitive + 10 harmless, used while tuning detectors. Post-tuning: full pipeline 20/20 flagged, 1 leak (`eval_holdout_glimmer.json`); code only 16/20 flagged, 2 leaks (`eval_holdout_codeonly.json`). |
| `corpus/final.yaml` | 40 | Sealed set: 26 sensitive + 14 harmless, written by a separate author. Run once per code change; the headline numbers below. |

## Metrics

`scripts/eval.py` gates every case in send mode, reviews HOLDs through
the real pipeline with a dry (no-network) upstream capture, and reports:

- **Catch rate**: sensitive cases whose final decision is not ALLOW.
- **Partial-leak rate**: sensitive cases where any sensitive value (in any
  encoded form) reached the captured outbound payload. Every leak so far
  is partial — a name, date of birth or street without context words —
  never a key or a full bundle.
- **False escalation**: harmless cases that did not finish ALLOW.
- **Share settled by code**: messages resolved by Tier 1 code alone
  (no judge, no person).
- **Latency p50/p95**: Tier 1 `tier1.scan` only (the judge takes ~10–20 s
  per review; see `metrics.latency_ms` in the JSON).

## Current numbers

Final set, run 3 with the Glimmer judge (`docs/results/eval_final_run3.json`,
Nov 3, `muse-glimmer:30b`, seed `demo-seed`): **26/26 caught, 3/26 partial
leaks (11.5 %), 7/14 harmless escalated, 70 % settled by code**
(27.5 % went to a person, 2.5 % settled by Glimmer + code).

Code only on the same set (`docs/results/eval_final_codeonly.json`,
`--judge none`): **26/26 caught, 3/26 partial leaks, 8/14 escalated**
(one more harmless message held; hard cases come to a person).

Tier 1 latency: **p50 0.45 ms / p95 0.94 ms over 107 messages**
(every inline-`input` case across the three corpora, best of 5 scans,
measured Nov 3; reruns vary with machine load).

Earlier final runs (`eval_final_run1_before_span_fix.json`,
`eval_final_run2.json`) and holdout runs (`eval_holdout_glimmer.json`,
`eval_holdout_codeonly.json`) are kept in `docs/results/` for comparison.
`setup/RESULTS.md` maps every README claim to its raw JSON field.

## Commands

`eval/run.py` is a thin wrapper around the existing tools:

```powershell
python eval/run.py --suite final --judge glimmer   # full pipeline (needs Ollama + muse-glimmer:30b)
python eval/run.py --suite final --judge none      # code only, no GPU needed
python eval/run.py --suite holdout --judge glimmer
python eval/run.py --suite cases --judge glimmer
python eval/run.py --bench                         # Tier 1 latency (scripts/bench_tier1.py)
python eval/run.py --quick                         # 10 live cases vs the hosted demo (setup/try_cases.py)
python eval/run.py --quick --local                 # 10 live cases vs 127.0.0.1:8765
```

`--judge` also accepts `llama` (`llama3.2:3b`). Eval runs write
`runs/eval_<date>_<suite>_<judge>.json` and the runner prints the path;
`--bench` and `--quick` print their tables to stdout. The direct
equivalents are `python scripts/eval.py --corpus <suite> --judge <judge>`,
`python scripts/bench_tier1.py`, and `python setup/try_cases.py [--local]`.
