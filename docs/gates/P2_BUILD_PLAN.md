# P2 — Build plan (gate doc)

Planned from the P1 design gate. Design: `P1_DESIGN.md`. Spike data: `P3_SPIKE_NOTES.md`.
Builder: Muse Code (one task per run), checked with pytest before each commit.

## Rules for every task
- Meta models only (`llama3.2:*`, `muse-glimmer:30b`, Meta Model API). Never use the other local models.
- Never commit a string in a real provider key format. Canaries are `SF_CANARY_<KIND>_<16hex>`,
  hex = HMAC-SHA256(seed, kind)[:16], generated at runtime. Tests build fake keys at runtime from split prefixes.
- Ollama at `http://127.0.0.1:11434` (not localhost), `num_ctx` 2048, `keep_alive` -1, `think` false, strip `<|eot|>`.
- Fail closed: any exception, timeout or invalid model JSON → HOLD. A model answer never removes a Tier 1 proven span.
- Logs store `sha256(value)[:12]` fingerprints, never raw values. Use plain `httpx` (no `openai` dependency).

## Layout
```
pyproject.toml   name=shadowfenced; deps fastapi uvicorn httpx pydantic pyyaml python-multipart python-docx pypdf
                 [dev] pytest pytest-asyncio playwright; script shadowfenced = shadowfenced.app:main
policy.yaml      fictional org "Corvane Labs": codenames, clients, figure cues, per-class action, user_can_keep, allowlist
.env.example     META_API_KEY= META_BASE_URL= META_MODEL= OLLAMA_URL=http://127.0.0.1:11434 SF_UPSTREAM=meta|ollama
                 SF_LOCAL_UPSTREAM_MODEL=llama3.2:3b SF_LIVE_MODEL=llama3.2:3b SF_JUDGE_MODEL=muse-glimmer:30b
                 SF_JUDGE_TIMEOUT_S=20 SF_LIVE_TIMEOUT_S=4 SF_LIVE_NUM_GPU=0 SF_CANARY_SEED=demo-seed SF_TIER2_MODE=live|replay|off
                 SF_LOG_PATH=runs/decisions.jsonl
src/shadowfenced/ config spans canary tier1 policy redact judge verifier engine holds ecs_log stats upstream extract app
                  static/{index.html,app.js,style.css}  (no build step)
corpus/cases.yaml   scripts/{make_files,eval,demo_scenes}.py   tests/ (one file per module + repo_guard, api, failclosed)
```
Delete: `agent.py tools.py remotes.py gate.py console.py watcher.py escalation.py metrics.py`, their tests, and
`spikes/spike1_*` (its fixed canary strings go with it).

## Tasks (order; Sat T1-T6 slice, Sun T7-T12, Mon T13-T16, Tue T17 + freeze, Wed T18 + submit)
- **T0 Model spike** — done (P3 notes): live 3B on CPU (`num_gpu` 0), Glimmer GPU+CPU, both resident.
- **T1 Rename + cleanup + repo guard.** `git mv` the old prototype package to `src/shadowfenced`; delete old modules/tests; rewrite
  pyproject, `.env.example`, `.gitignore` (+`runs/`), README header. `tests/test_repo_guard.py`: scan `git ls-files`
  for full-length provider key shapes (prefixes built at runtime) and for a tracked `.env`. Accept: `test_package_imports`,
  `test_no_provider_key_shapes_in_tracked_files`, `test_no_env_file_tracked`, pytest green.
- **T2 Canaries + Tier 1 secrets** (`canary.py spans.py tier1.py`). Proven: canary in exact/b64/hex/rot13/reversed/url/
  split forms, any `SF_CANARY_` token, provider key shape, `password|passwd|secret|token\s*[:=]\s*value`. Ambiguous:
  entropy token ≥20 chars, Shannon > 4.0, not allowlisted (git sha, UUID). Tests incl. `test_tier1_under_20ms_on_4kb`.
- **T3 Policy + PII + codenames + beacon cues** (`policy.yaml policy.py tier1.py`). Email (allow example.*), phone, SSN
  shape, `EMP-\d{6}` near a name, Luhn card; policy words case/hyphen-insensitive with word boundaries; beacon cue =
  secret noun + location token in one sentence → ambiguous. UTF-16 offset test with emoji.
- **T4 Redaction + Tier-1-only engine** (`redact.py engine.py`). Severity BLOCK > HOLD > REDACT > ALLOW. Canary → BLOCK;
  secret/PII/codename → REDACT per policy; ambiguous → HOLD. Keep only where `user_can_keep` (never secrets/canaries),
  logged. Tolerant re-hydration regex `\[\s*(SECRET|PII_[A-Z]+|CODENAME|CLIENT|BEACON)[\s_-]*(\d+)\s*\]`.
- **T5 API + upstream** (`app.py upstream.py config.py`): `/api/scan`, `/api/send`, `/v1/chat/completions`, `/api/health`,
  fail-closed wrapper. Tests with httpx MockTransport: allow forwards, redact hides value from upstream, rehydrates,
  block 403, hold 409, stream=true single SSE chunk, upstream down → HOLD.
- **T6 UI thin slice** (`static/*`): textarea + mirrored backdrop `<mark>`s, Tier 1 scan debounced 250 ms, issue list with
  Redact/Keep/Why, send → banner + "What the AI saw". **End-to-end checkpoint.**
- **T7 ECS log + stats**: hash chain, tamper detection, no raw values, tier counts + p50/p95; `/api/stats`, `/api/log/verify`.
- **T8 Tier 2 judge client** (`judge.py`): pydantic `Judgement{cls: secret|pii|business|beacon|benign, sensitivity 0-1,
  is_beacon, span ≤200, confidence 0-1, reason ≤200}`; Ollama `/api/chat` with `format` schema, temperature 0, seed 7;
  ambiguous span + ~300 chars context only; any failure → None; `SF_TIER2_MODE=replay` for CI only; startup warm-up.
- **T9 Tier 3 verifier + engine integration**: span must be an exact substring (code finds offsets); secret claim confirmed
  by decode-to-canary / key shape / entropy; pii/business by regex or policy word; beacon confirmed only with a location
  token and a nearby value, else HOLD; model-benign resolves ALLOW only if confidence ≥ 0.8 and sensitivity < 0.3.
  `test_failclosed.py`: each tier raising → HOLD; judge timeout → HOLD.
- **T10 HOLD evidence card**: `/api/holds/{id}`, `/rule`; approve forwards redacted text only; deny sends nothing.
- **T11 Tier 2-live underlines**: 3B on CPU, ambiguous spans, 4 s timeout, purple dotted "model suspicion" after 1.2 s pause.
- **T12 Upload**: `extract.py` (txt md csv docx pdf, 2 MB), `/api/upload`, file chip; `make_files.py` writes demo files to
  `runs/files/` at runtime. Unscannable → HOLD on send.
- **T13 Provider switcher + Meta upstream**: "Meta Muse Spark (cloud)" vs "Llama 3.2 3B via Ollama (local stand-in)";
  no key → Meta greyed out.
- **T14 Corpus** (`corpus/cases.yaml`, templates only: `{{canary:db_password|base64}}`, `{{fake_key:aws}}`,
  `{{entropy:32}}`, `{{file:customers.csv}}`): ~40 cases, 10 secrets, 7 PII, 4 business, 3 beacons, 16 benign look-alikes.
- **T15 Eval** (`scripts/eval.py --judge glimmer|llama|none`) → `runs/eval_<date>.json`: leak_rate, catch_rate next to
  false_escalation_rate, hold_rate, per-tier latency, resolved_by_tier, per_case, ablation (Glimmer lift).
- **T16 Tuning**: false escalation ≤ 15 %, leak 0 on non-HOLD cases → `docs/gates/P5_EVAL.md`.
- **T17 Playwright scenes + recording** (`scripts/demo_scenes.py`), 3 clean runs in a row.
- **T18 Polish**: README (diagram, run steps, metrics table), Muse session evidence, logged-out repo check.

## API contract (summary)
- Offsets in UTF-16 code units (`start`,`end`) plus code points (`cp_start`,`cp_end`).
- `/api/scan {text, mode: tier1|live}` → `spans[{span_id, start, end, cls, subclass, tier, proof: code|model, message, why,
  suggested_action, can_keep, confidence}], latency_ms`.
- `/api/upload` (multipart) → `{file_id, name, status: clean|flagged|unscannable, spans, summary}`; others 415.
- `/api/send {messages, file_ids, choices, provider}` → `{decision, decision_id, sent_text, answer, answer_highlights,
  evidence, resolved_tier, latency_ms, unmatched_placeholders}`.
- `/v1/chat/completions`: ALLOW/REDACT → 200 + `shadowfenced` field + `X-Shadowfenced-Decision`; BLOCK → 403
  `shadowfenced_block`; HOLD → 409 `shadowfenced_hold` with `review_url`; non-text parts → HOLD; upstream down → 502.
- Placeholder map server-side per decision, in memory, 15 min TTL, never logged.

## UI test hooks (`data-testid`)
`composer send mark-<cls> issue-card btn-redact btn-keep btn-why why-text file-input file-chip banner(data-decision)
ai-saw-toggle ai-saw answer evidence-card btn-approve btn-deny provider-select stats-tier1 stats-holds log-ok`

## Demo scenes (3:00; target 2:50)
S0 title/hook · S1 benign → ALLOW · S2 config paste: canary + email underlined, Why, BLOCK; S2b email only → REDACT,
AI saw `[PII_EMAIL_1]`, answer re-hydrated · S3 base64 canary in a debug log → BLOCK · S4 customers.csv upload → chip
flagged → REDACT · S5 beacon → purple underline → Glimmer → HOLD card → Deny, upstream untouched · S6 switch provider →
same result · S7 stats + eval numbers (from the eval file only) · S8 hash chain verified, "the same engine guards agents".
