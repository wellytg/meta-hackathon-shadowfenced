# Fine-tune: distilling Glimmer into a fast 3B judge

Muse Glimmer (30B, local via Ollama) is the accurate judge: it reads a
held message and returns a typed verdict (`cls`, `is_beacon`,
`sensitivity`, `confidence`, `span`), which Tier 3 verifies in code
before anything is sent. But it is slow (~11 s a case on this PC) and
heavy. The 3B model answers in well under a second but judges poorly
out of the box.

This pipeline distils Glimmer's judgements into Llama 3.2 3B with QLoRA,
so the small model learns the org's rules as a fast, rule-aware
first-pass judge. Glimmer stays the authority for review; the tuned 3B
only moves clear-cut cases faster.

**All data is fully synthetic.** The org (Corvane Labs), codenames,
clients, people and credentials are invented. Secret examples use
`SF_CANARY_` canaries minted at generation time; a hygiene filter drops
any candidate holding a real-format provider key shape, and the test
suite asserts this on every run.

## Pipeline

```text
policy.yaml ──► ft_generate.py ──► runs/ft/raw.jsonl
   (+policy.local.yaml)   (Muse Spark via Meta upstream client)

raw.jsonl ──► ft_label.py ──► runs/ft/labeled.jsonl  (judge agrees)
                            runs/ft/disputed.jsonl (human review)

labeled.jsonl ──► ft_split.py ──► train.jsonl / val.jsonl / test.jsonl
                   (seeded 80/10/10)   (+ split.json manifest)

train+val ──► ft_train.py ──► runs/ft/adapter/   (QLoRA, 4-bit, r=16)

test ──► ft_eval.py ──► runs/ft/eval.json + eval.md  (base vs tuned vs Glimmer)
```

1. **Generate** (`scripts/ft_generate.py`): reads `policy.yaml` (and
   `policy.local.yaml` when present) for codenames, clients and figure
   cues, then asks Muse Spark through the existing Meta upstream client
   (`shadowfenced.upstream.chat_sync`, `provider="meta"`) for short
   workplace messages: leaks per class (secret, personal, business,
   beacon) that use the rules, near-misses that look risky but are safe
   (lowercase codename prose, placeholder credentials, allowlisted
   shapes), and plainly safe messages. Rows are deduplicated, capped at
   500 chars, and screened for real key shapes.
2. **Label** (`scripts/ft_label.py`): runs the existing Tier 1 scan plus
   the Glimmer judge (local Ollama) over each raw row. Agreements keep
   the typed verdict; judge failures, invalid verdicts and
   intended-vs-judged class mismatches go to `disputed.jsonl`.
3. **Split** (`scripts/ft_split.py`): deterministic seeded shuffle,
   80/10/10. The test split is written separately and `ft_train.py`
   never loads it.
4. **Train** (`scripts/ft_train.py`): QLoRA SFT of
   `meta-llama/Llama-3.2-3B-Instruct` (transformers + peft +
   bitsandbytes, 4-bit NF4, LoRA r=16) on the chat format
   "message → verdict JSON". Batch 1 × 16 accumulation, max length
   1024, gradient checkpointing, paged 8-bit AdamW: sized for one
   12 GB GPU. Saves the adapter to `runs/ft/adapter/`.
5. **Eval** (`scripts/ft_eval.py`): on the test split, base 3B vs tuned
   3B vs Glimmer: class accuracy, beacon F1, leak rate (sensitive rows
   judged safe), false-alarm rate, valid-JSON rate, p50/p95 latency.

## Exact commands

```powershell
pip install -e ".[finetune]"
python scripts/ft_generate.py                     # needs META_API_KEY (Spark)
python scripts/ft_label.py                        # needs local Ollama + muse-glimmer:30b
python scripts/ft_split.py --seed 7
python scripts/ft_train.py                        # needs GPU + Hub access
python scripts/ft_eval.py                         # needs tuned adapter + Ollama
python -m pytest -q                               # no network, no GPU
```

Only the test suite runs without network/GPU. Generation needs the Meta
API, labelling/eval need the local Ollama daemon, training needs a GPU.

## Metrics to expect

Distillation succeeds when the tuned 3B closes most of the gap to
Glimmer on class accuracy and leak rate while keeping latency near the
base 3B (~1 s vs ~11 s). If leak rate does not move, add more
near-miss/beacon rows and re-run; if valid-JSON rate drops, the verdict
schema in the training completions diverged from the judge schema.
