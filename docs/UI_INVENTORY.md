# UI inventory (T31)

Generated from the auditor's baseline and updated for the T31 chat shell,
themes, and landing page. Python behaviour and API contracts are unchanged.

## 1. Workspace root

`design-ref` (AGENT_BRIEF, DESIGN_SYSTEM, INTERACTION_SPEC, PAGE_BLUEPRINTS,
ACCEPTANCE, tokens.css, VIDEO_AUDIT, frames x14), `src/shadowfenced` (app.py,
rules.py, engine/tier1/holds/policy/redact/...), `static` (index.html, app.js,
rules.js, style.css, rules.css, themes.css, welcome/, assets badge.svg
block-chip.svg), `tests` (~30 files), corpus, scripts, deploy, docs/gates,
policy.yaml.

## 2. Design-ref contract (summary, not copied)

- Fluid Containment: warm chalk/paper/ink, single membrane motif, no
  purple-gradient/orb/shield/neon grids/red flashes.
- States map to idle/inspecting/released/redacted/review_required/blocked/
  failed; unknown -> neutral pending, never safe.
- Composer warm paper + boundary edge; released 180-260ms; redacted
  260-420ms span-contract + count; review 200-350ms amber firm + anchored
  choices, no auto-send; blocked one 160-240ms local recoil, persistent text;
  reduced-motion kills transforms; live regions polite/assertive.
- Pages: landing (nav, hero w/ labeled illustrative sample, labeled
  simulation explainer, how-it-works, trust, screenshots/FAQ/CTA) + app
  (header/provider/security/settings, conversation + one compact marker/turn,
  composer+attach, collapsible inspector drawer, inline anchored review,
  mobile bottom composer + drawer).
- ACCEPTANCE: identity consistency, warm light-first, responsive
  390/768/1024/1440, real-state binding, no pre-confirm sent, review/block
  never auto-send, no raw-secret leaks, attachments/provider preserved,
  fail-closed unknown, keyboard/reduced-motion/text-not-color, contrast/focus,
  evidence screenshots + test logs.

## 3. Routes (src/shadowfenced/app.py; rules via rules.py router)

GET / -> StaticFiles html=True -> static/index.html; GET /welcome/ ->
static/welcome/index.html; GET /api/health {status,live_hints};
GET /api/hosted {hosted,banner}; POST /api/scan {text,mode} ->
{spans,latency_ms,trace}; POST /api/send {messages,choices,file_ids,
provider} -> send-response; POST /v1/chat/completions OpenAI-compat,
BLOCK->403 HOLD->hold-response; GET /api/holds/{hold_id}; POST
/api/holds/{hold_id}/rule {approve}; GET /api/keyguard
{keys,protected_keys,hits} names/counts only; GET /api/stats;
POST /api/upload multipart -> {file_id,name,kind,chars,status,spans,
summary,summary_text}; GET /api/providers {providers[{id,label,available}],
default}; POST /api/provider {id}; GET /api/log/verify {ok,events};
GET/POST /api/rules (rules.py) {kind:codenames|client|pattern,
value,action:block|redact|review,note}; DELETE /api/rules/{rule_id}.

## 4. API shapes

- Span (span_to_dict): span_id, start/end (UTF-16), cp_start/cp_end, cls,
  subclass, tier (1 proven / 2 suspicion), proof, status, message, why,
  suggested_action, can_keep, confidence.
- /api/send -> {decision:ALLOW|REDACT|BLOCK|HOLD, decision_id, sent_text,
  answer, answer_highlights, user_message (only string rendered in banner),
  evidence{spans,reasons}, resolved_tier, latency_ms, unmatched_placeholders,
  provider (server-resolved), trace[], review_url if HOLD}.
- HOLD poll card fields: proven_by_code[].text,
  model_suspicion_only[{span,judgement{cls,confidence}}], redacted_preview,
  preview_marks[{start,end,kind}], status
  (reviewing/redacted_sent/released/needs_human/denied/approved), title,
  user_message, sent_text, answer, provider.
- Upload: {file_id,name,kind,chars,status:clean|flagged|unscannable,spans,
  summary,summary_text}; unscannable/unknown -> send HOLDs.

## 5. UI states (bound to server)

- Thread: empty-hint -> user bubble -> banner[data-decision] -> compact
  receipt[data-decision] (details/summary, expands to why) ->
  ai-saw-toggle/ai-saw -> answer -> evidence-card; checking + send disabled
  while pending; composer preserved until success then cleared. Banner text
  from server user_message only. Membrane (composer-block[data-membrane]:
  idle/inspecting/released/redacted/review/blocked/unknown) follows the same
  server decision; unknown -> neutral, never "sent".
- Receipt labels from the send response: ALLOW "Sent", REDACT "N sensitive
  details protected", HOLD "Review required", BLOCK "Blocked", anything else
  neutral "Not sent — waiting on the server". The why body carries only
  user_message + resolved tier + trace step names — never raw text, span
  values, ids, or hashes.
- Evidence card: spinner+elapsed, Proven-by-code vs Model-suspicion-only
  columns, redacted preview, approve (Send without flagged part) / deny
  (Don't send) via /api/holds/rule; polls to 180s then offers choice.
- Issue cards: per-span Redact/Keep (Keep disabled if !can_keep)/Why
  (tier-proof-why-tier1ms); choices default empty, untouched redacts by
  default.
- Composer underline: backdrop marks from /api/scan tier1 (250ms debounce) +
  tier2 live hints only if /api/health live_hints (1200ms pause).
- Header: provider-select fed by /api/providers, header-pill static
  local-check text, theme-select (classic/fluid/fluid-dark; only the name is
  kept in localStorage), Rules button + Descriptive-mode toggle (localStorage,
  aria-pressed), sf-trace list after each banner (hidden unless descriptive
  on), rules drawer dialog (built-in readonly + custom list, add form,
  sf-error).
- Sessions sidebar: New chat + per-chat list. Sessions live in page memory
  only — never localStorage, never fetched, never sent anywhere.
- Welcome page: static explainer (fence SVG, tag-arrives-value-does-not),
  three-step story with inline SVG+CSS panels each labeled Illustration,
  four-tier how-it-works matching the real pipeline, what-stays-local +
  limits, CTAs to /. No live gate; CTAs to /.

## 6. Full testid/id catalog

- index.html static: hosted-banner, header-pill, empty-hint,
  composer#composer, send#send, attach (paperclip#paperclip), file-input#
  file-input, provider-select#provider, theme-select#theme,
  new-chat#new-chat, chat-list#chat-list, details-drawer#details-drawer,
  composer-block#composer-block[data-membrane], membrane-line,
  stats-tier1#stats-tier1, stats-holds#stats-holds,
  stats-held-resolved#stats-held-resolved, log-ok#log-badge; ids: thread,
  backdrop, issues, checking, file-chips, decision-counts, tier-bar,
  last-latency.
- app.js runtime: banner[data-decision], receipt[data-decision]
  (ALLOW|REDACT|HOLD|BLOCK|UNKNOWN), ai-saw-toggle, ai-saw, answer,
  evidence-card, btn-approve, btn-deny, btn-redact, btn-keep, btn-why,
  why-text, file-chip[data-status], mark-{cls}.
- rules.js runtime: sf-trace, sf-drawer (role=dialog), sf-custom-rules,
  sf-value, sf-add, sf-error, sf-rules-btn, sf-descriptive-toggle
  (aria-pressed).
- welcome/index.html: hosted-banner, open-console, open-console-main;
  story-a/b/c titles/descs, fence-title, fence-desc, dots.
- Recorder hooks kept: composer/send/answer/banner/btn-why/btn-approve/
  btn-deny/evidence-card/file-input/ai-saw-toggle (+ new-chat, receipt,
  theme-select, details-drawer).

## 7. Tests mapping

- test_api: route table + review_url prefix, scan/send shapes.
- test_ui_review_t6: evidence-card/approve/deny wiring, /api/holds/
  endpoints, banner/receipt hooks.
- test_t24_descriptive: sf-trace testid after banner, descriptive toggle
  fetch-/api/send interception.
- test_t21_rules: /api/rules CRUD.
- test_upload_api + test_hosted + test_welcome_page:
  upload/file-input/provider-select/hosted-banner/open-console.
- test_review_fixes/test_api/test_hosted: banner/ai-saw-toggle/ai-saw/
  answer/btn set/header-pill/empty-hint.
- test_t31_shell (new): theme switch + localStorage key, ids present under
  every theme (same markup), chats memory-only, receipt mapping incl.
  unknown, welcome hero/story/how/local sections, docs presence.

## 8. Deltas vs the auditor baseline (what T31 built + T31b shell round)

- T31b header: one slim 56–64px bar — wordmark left, compact model pill in
  the centre ("Model:" + provider-select), details-drawer toggle + settings
  menu right (Theme select, Descriptive-mode toggle, Rules button relocated
  into the menu at runtime by an inline script; rules.js itself unchanged).
  No full-width selects, no stacked labels.
- T31b receipts: one compact per-turn receipt line (icon + short label +
  "Why" disclosure) in the message footer; colour only on the icon and the
  thin left rule. The receipt keeps class "banner" + the banner hook so
  recorders and the descriptive trace pairing keep working; the upload
  failure note keeps data-testid "banner". Decision text updates the receipt
  label in place (finishBanner) instead of wiping it.
- T31b details drawer: opens from the header toggle; shows the selected
  turn (decision, reasons, redaction summary with placeholder labels,
  descriptive trace, model, timing) plus session counts and the log-chain
  badge. Empty state ("Select a turn to see details.") when nothing is
  selected. Receipts are clickable to select their turn.
- T31b membrane: vertical line on the composer send edge and between the
  conversation and the drawer, labelled PRIVATE / EXTERNAL in small caps on
  wide screens. Release eases, review firms amber and still, block recoils
  once (220ms), unknown stays neutral; reduced motion collapses to instant
  colour/text changes. Classic hides the membrane and keeps hard outlines.
- T31b landing: hero wrapped in .hero (asymmetric two-column in Fluid);
  story steps are three side-by-side panels on desktop (grid) and stacked
  on mobile, each with an inline SVG showing a person and an agent: a full
  request (email + contract clause + .env API key), the highlighted values
  leaving (b), and the membrane with placeholders crossing (c).

- Chat-app shell: sidebar + top picks + central column + bottom composer +
  collapsible details drawer; receipts + membrane bound to /api/send.
- Themes: classic default + fluid / fluid-dark via [data-theme]; see
  docs/THEMES.md.
- Landing: hero "Your words. Your boundary.", 3-step story with labeled
  Illustration panels, four tiers as the code works, trust + limits.
- No Python changes; no API contract changes; no network/font additions.
