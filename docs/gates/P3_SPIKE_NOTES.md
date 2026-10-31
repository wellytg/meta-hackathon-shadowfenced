# P3 — Spike notes (running log)

## 2026-10-31 — Live tier speed (llama3.2:3b, Ollama 0.40.13, RTX 5070)
- Default Ollama context (131k) loaded an 18 GB runner, 45% on CPU: ~2.4-6 s per call. Fix: `num_ctx: 2048` → 2.3 GB, 100% GPU.
- Windows `localhost` adds ~2 s per request (IPv6 attempt first). Fix: always call `http://127.0.0.1:11434`.
- With both fixes: **0.18-0.37 s per typed JSON decision** (prompt ~100 tokens, 17-56 output tokens). Schema-constrained
  output costs ~0.1 s extra. Live underline is feasible.
- Judgement is weak: a name + DOB + account number was labelled `business`, sensitivity 0. Decision: Tier 1 code
  checks carry the live underline; the live model adds hints only; Glimmer decides at send.

## 2026-10-31 — Send-time judge (muse-glimmer:30b, Q4, 17 GB, 47% CPU / 53% GPU, num_ctx 2048, think off)
- Cold load ~25 s (do a warm-up call at server start). Warm: **6-12 s per decision** (~5 tok/s generation).
- Judgement is clearly better than the 3B: caught the name + DOB + account as `personal` (0.7) and the
  "removed the old database password from settings.py" message as a beacon. Banana bread → `none`.
- Missed the fictional codename "Project Bluefin" (cls `none`). Codenames must come from the policy word list (Tier 1).
- Output carries a trailing `<|eot|>` token after the JSON. Strip before parsing.
- Decision: Glimmer only at send, only on spans Tier 1 marks ambiguous, with a timeout → HOLD. Keep the
  prompt short and `num_predict` ≤ 80. The UI shows a "checking with local Glimmer…" state.

## 2026-10-31 — Co-residency (T0 remainder)
- With both on the GPU, loading one model evicts the other (cold reload ~25 s for Glimmer).
- Fix: live model on CPU only (`num_gpu: 0`): llama3.2:3b at **1.4-3.3 s** per decision, and both stay resident
  (`keep_alive: -1`). Glimmer stays 6-12 s warm. Settings: `SF_LIVE_NUM_GPU=0`, `SF_LIVE_TIMEOUT_S=4`, `SF_JUDGE_TIMEOUT_S=20`.

## 2026-10-31 — Judge under real load (after T8-T9)
- With the T8 schema (incl. a free-text `reason`), Glimmer wrote long reasons, ran past `num_predict` 120 and the
  20 s timeout, and skipped `is_beacon`: every send-time judgement failed → HOLD (fail-closed worked).
- Host CPU at 100 % from other desktop apps: Glimmer fell to ~2-3 tok/s (from ~5). Minimal schema
  (cls, is_beacon, sensitivity, confidence, span ≤ 8 words; no reason) → 30-54 tokens, 13-24 s.
- Decision: asynchronous review. Ambiguous → instant HOLD; Glimmer reviews in the background (60 s timeout);
  code-verified → auto-send redacted; confident benign → release; otherwise human. The UI never waits on Glimmer.

## 2026-10-31 — Meta Model API upstream
- `api.meta.ai` failed TLS verification from Python: an HTTPS-scanning antivirus re-signs HTTPS for scanning,
  and its root is in the Windows store, not in certifi. Fix: `truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)` as the
  httpx `verify` (verification stays on). Dependency: `truststore`.
- `muse-spark-1.3` via `https://api.meta.ai/v1/chat/completions`: 200 in 0.7 s. With `max_tokens` 20 the content was
  empty because the model reasons first (275 reasoning tokens of 289). Do not cap `max_tokens` low; omit it or use ≥ 1024.
