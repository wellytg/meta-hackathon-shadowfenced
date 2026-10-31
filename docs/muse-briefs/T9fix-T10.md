# Muse brief T9-fix + T10 — Minimal judge schema, asynchronous Glimmer review, HOLD evidence card

Read first: docs/gates/P3_SPIKE_NOTES.md (last section), docs/gates/P2_BUILD_PLAN.md (T10, UI hooks),
src/shadowfenced/{judge,verifier,engine,app}.py and static/app.js.

1. judge.py: the schema sent to the model (`format`) is minimal: {cls enum [secret,pii,business,beacon,benign],
   is_beacon bool, sensitivity number, confidence number, span string}, all required, no `reason`, no min/max keywords
   (validate ranges in pydantic after parsing; clamp nothing, reject out-of-range). System prompt: "Classify text a user
   is about to send to an outside AI. beacon = reveals where secrets or personal data are stored or were moved.
   span = the shortest exact sensitive phrase copied from the text, at most 8 words, or empty. JSON only."
   num_predict 80. `reason` becomes a code-built string (from verifier why), not model output. Send timeout default 60 s.
2. engine.gate(mode="send") no longer calls the judge inline. Ambiguous spans -> immediate HOLD (resolved_tier 4,
   status "reviewing") with evidence {proven_by_code, pending:[span texts]}. Return fast.
3. holds.py (new): in-memory HoldStore {hold_id(=decision_id): text, messages, provider, spans, evidence, status
   reviewing|released|redacted_sent|needs_human|denied|approved, judgements, answer, sent_text, timestamps}.
   `start_review(hold)` runs in a background thread: for each pending span: judge (send model) -> verify.
   All confirmed -> redact those spans too and auto-send (status redacted_sent, store answer, rehydrated).
   All model-benign with confidence >= 0.8 and sensitivity < 0.3 and no Tier 1 proven secret -> release and send
   (status released). Anything else, judge None, or exception -> needs_human with model_suspicion_only entries
   {span, judgement, why_unverified}. Never send a canary. Log each outcome.
4. Routes: GET /api/holds/{id} -> card {status, proven_by_code, model_suspicion_only, pending, redacted_preview,
   answer?, sent_text?, user_message}; POST /api/holds/{id}/rule {approve: bool, reason?}: deny -> sends nothing
   (status denied); approve -> forwards the redacted text only (proven spans stay redacted), returns /api/send shape.
   /v1/chat/completions keeps returning 409 with review_url for HOLD.
5. UI (static/app.js, style.css): on HOLD render `evidence-card` under the banner: two columns "Proven by code" and
   "Model suspicion only"; while status reviewing show "Local Muse Glimmer (30B, on this PC) is reviewing…" with a
   spinner and elapsed seconds; poll GET /api/holds/{id} every 1 s until not reviewing. Then: redacted_sent/released ->
   update banner to the final decision and show the answer bubble; needs_human -> show the judgement (cls, confidence,
   span) and buttons `btn-approve` ("Send redacted") and `btn-deny` ("Don't send"). Keep all testids.
6. Live underline (T11): on a 1200 ms typing pause call /api/scan mode=live; render tier-2 suspicion spans as purple
   dotted marks with tooltip "Model suspicion (Llama 3.2, local) — checked again at send". Drop stale responses.
Tests: update test_judge (schema has no reason; num_predict 80), test_failclosed (send path returns HOLD fast without
calling judge), new tests/test_holds.py (auto-redact-send when verified; release when benign-confident; needs_human on
judge None; deny sends nothing; approve forwards redacted only; canary never sent). Use MockTransport and call the
review function synchronously in tests. Run `python -m pytest -q` until green. Do NOT commit. Summarise.
