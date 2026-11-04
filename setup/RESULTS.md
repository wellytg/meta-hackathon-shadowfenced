# Where every claimed number comes from

Each row maps a number the root README claims to the raw file in
`docs/results/` and the command that reproduces it. Eval commands need
Ollama with `muse-glimmer:30b` (about 4–6 minutes per 40-case run on an
RTX 5070) and write to `runs/`; the committed files are the recorded
outputs. All corpora are synthetic (fictional org "Corvane Labs").

## Final test set (run 3, current numbers)

File: `docs/results/eval_final_run3.json` (judge `muse-glimmer:30b`,
seed `demo-seed`, 2026-11-03). Reproduce with:

```powershell
python scripts/eval.py --corpus final --judge glimmer
```

| README claim | Raw field | Value |
|---|---|---|
| 70 % settled by code | `metrics.resolved_by_tier["1"]` | 0.7 (28 of 40) |
| 2.5 % by Glimmer + code (~16 s) | `metrics.resolved_by_tier["3"]`, `metrics.latency_ms["3"].p50` | 0.025 (1 case), 16180.89 ms review |
| 27.5 % went to a person | `metrics.human_rate` (`final == HOLD`) | 0.275 (11 of 40) |
| 26 of 26 sensitive flagged | `metrics.catch_rate`, `metrics.n_sensitive` | 1.0 over 26 |
| 3 of 26 leaked (11.5 %) | `metrics.leak_rate`; cases `f-pii-3`, `f-pii-4`, `f-pii-7` | 0.1154 |
| 7 of 14 harmless held | `metrics.false_escalation_rate`, `metrics.n_benign` | 0.5 over 14 |
| Leaks are partial (1, 2 and 2 values) | re-gate the 3 cases (see below) | name / name+birth date / name+street |

The leak detail is not stored per value in the JSON (it records
`leaked: true` per case). It was checked by re-gating the three cases
with the current code: `f-pii-3` leaks 1 value (a name), `f-pii-4`
leaks 2 (a name and a birth date), `f-pii-7` leaks 2 (a name and a
street address). Values are synthetic corpus data; only counts and
kinds are quoted here.

Earlier runs on the same set: `docs/results/eval_final_run1_before_span_fix.json`
and `docs/results/eval_final_run2.json` (both 2026-10-31, identical
metrics: 55 % / 7.5 % / 37.5 %, 3 of 26 leaks, 4 of 14 held). Run 2
followed a parser fix (long Glimmer spans were being discarded) found
while recording the demo. Run 3 is the current number.

## Tier 1 code latency

README claim: Tier 1 costs p50 0.45 ms / p95 0.94 ms over 107 corpus
messages. Reproduce with:

```powershell
python scripts/bench_tier1.py
```

The script expands every inline-`input` case in `corpus/cases.yaml`,
`corpus/holdout.yaml` and `corpus/final.yaml` (40 + 30 + 40 cases minus
the 3 file-based cases in `cases.yaml` = 107 messages, seed
`demo-seed`), warms up once, and keeps the best of 5 scans per message.
The 0.45/0.94 reference was measured on an RTX 5070 box; verification
reruns on the same box gave p50 0.39–0.52 and p95 0.87–1.17 depending
on background load — the script reproduces the claim within normal
machine noise. (Do not use the `latency_ms["1"]` block of
`eval_final_run3.json` for this: those gate times include synchronous
person-search model calls under eval load.)

## Holdout set (tuning set)

README claim: `corpus/holdout.yaml` (30 cases) went 40 % → 5 % leaks,
65 % → 100 % flagged, 20 % → 30 % harmless held (code-only before
tuning → full pipeline after).

- After tuning: `docs/results/eval_holdout_glimmer.json`
  (`leak_rate` 0.05, `catch_rate` 1.0, `false_escalation_rate` 0.3).
  Reproduce with `python scripts/eval.py --corpus holdout --judge glimmer`.
- `docs/results/eval_holdout_codeonly.json` is the post-tuning
  code-only point (`--judge none`): 10 % / 80 % / 20 %.
- The before-tuning code-only numbers (40 % / 65 % / 20 %) predate the
  saved runs: no raw file in `docs/results/`. They are quoted from the
  tuning notes, not from a committed eval file.

## In-sample set

README claim: `corpus/cases.yaml` (40 cases) scores 0 % leaks, 100 %
flagged, 0 % harmless held — in-sample, not a fair test. No raw file is
committed. Reproduce with `python scripts/eval.py --corpus cases
--judge glimmer`.

## Test count

README claims the pytest total. Reproduce with:

```powershell
python -m pytest -q -p no:cacheprovider
```

The count at the time of writing is in the README; it grows as tests
are added.
