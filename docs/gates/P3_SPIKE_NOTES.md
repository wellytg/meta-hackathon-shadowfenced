# P3 — Spike notes (running log)

## 2026-10-31 — Live tier speed (llama3.2:3b, Ollama 0.40.13, RTX 5070)
- Default Ollama context (131k) loaded an 18 GB runner, 45% on CPU: ~2.4-6 s per call. Fix: `num_ctx: 2048` → 2.3 GB, 100% GPU.
- Windows `localhost` adds ~2 s per request (IPv6 attempt first). Fix: always call `http://127.0.0.1:11434`.
- With both fixes: **0.18-0.37 s per typed JSON decision** (prompt ~100 tokens, 17-56 output tokens). Schema-constrained
  output costs ~0.1 s extra. Live underline is feasible.
- Judgement is weak: a name + DOB + account number was labelled `business`, sensitivity 0. Decision: Tier 1 code
  checks carry the live underline; the live model adds hints only; Glimmer decides at send.
