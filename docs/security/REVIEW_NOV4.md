# Security review — Nov 4 (read-only review of app, proxy, hosted demo, Docker, extension)

Scope: whole repo. Items 1-7 are fixed in this branch with the smallest safe
change plus a test each (all in `tests/test_t81_secfix.py`). Items 8-14 are
documented known limits (not fixed here). Item 15 groups the areas reviewed
and found fine. `README.md` and `src/shadowfenced/static/` were not touched.

Status values: FIXED in this branch / KNOWN LIMIT / FINE.

| # | Finding | Severity | Status | File(s) |
|---|---------|----------|--------|---------|
| 1 | Hosted `/api/log/events` listed `decision_id`s, which double as hold ids, so one visitor could open (`GET /api/holds/{id}`) and rule on (`POST /api/holds/{id}/rule`) another visitor's held messages. Fixed by refusing the endpoint in hosted mode via `_refuse_hosted_storage`; hold ids stay uuid4 hex (unguessable). Test: `test_hosted_log_events_exposes_no_decision_ids`. | High | FIXED in this branch | `src/shadowfenced/app.py` |
| 2 | `/api/send` gated only user text; system/assistant/tool messages went to the cloud unchecked. Fixed by gating every non-user role (plus user tool-call arguments) with the `/v1` engine `_gate_chat_messages`; any BLOCK/HOLD there decides the send, REDACTs merge into one decision/placeholder map, and the roles are re-checked jointly so a bundle split across roles still blocks. Tests: `test_send_blocks_key_in_system_message`, `test_send_holds_ambiguous_tool_message`, `test_send_blocks_key_in_user_tool_arguments`, `test_send_redacts_email_in_system_message`, `test_send_merges_placeholders_across_roles`. | High | FIXED in this branch | `src/shadowfenced/app.py` |
| 3 | `/v1` forwarded unchecked message fields (`name`, `function_call`, …), and plain-string content parts were gated but never rewritten, so raw text was forwarded. Fixed by forwarding only `role`, `content`, `tool_calls`, `tool_call_id`, and by writing redacted strings back into plain-string parts. Tests: `test_v1_forwards_only_safe_message_fields`, `test_v1_rewrites_plain_string_content_parts`. | High | FIXED in this branch | `src/shadowfenced/app.py` |
| 4 | `POST /api/provider` had no hosted guard: one visitor changed the provider for everyone. Fixed by refusing in hosted mode like the other storage endpoints. Test: `test_hosted_set_provider_refused`. | Medium | FIXED in this branch | `src/shadowfenced/app.py` |
| 5 | Docker exposed the console (`8765:8765` on all interfaces) and loopback serving had no host check (DNS rebinding). Fixed: compose maps `127.0.0.1:8765:8765`; Dockerfile keeps `0.0.0.0` (needed inside the container) with an explanatory comment; local mode adds `TrustedHostMiddleware(allowed_hosts=["127.0.0.1","localhost","testserver"])`, skipped entirely in hosted mode. Tests: `test_compose_binds_console_to_loopback`, `test_dockerfile_keeps_container_bind_with_comment`, `test_trusted_host_middleware_configured_locally`, `test_trusted_host_rejects_rebinding_locally`, `test_trusted_host_allows_public_host_in_hosted_mode`. | High | FIXED in this branch | `compose.yaml`, `Dockerfile`, `src/shadowfenced/app.py` |
| 6 | Uploads read the whole body before the size check. Fixed: the declared `file.size` past the cap answers without reading the body at all, otherwise a single read of at most cap+1 bytes bounds memory (console route and `ext_api` scan-file alike; audio keeps its 32 MB transcription cap). Tests: `test_upload_limit_for_picks_cap`, `test_upload_declared_oversize_skips_body_read`, `test_read_upload_capped_bounds_read`, `test_read_upload_capped_honours_declared_oversize`, `test_ext_scan_file_reads_at_most_cap_plus_one`, `test_ext_scan_file_declared_oversize_skips_body_read`, `test_upload_route_rejects_oversize_file`. | Medium | FIXED in this branch | `src/shadowfenced/app.py`, `src/shadowfenced/ext_api.py` |
| 7 | `redact.rehydrate` restored real values inside link targets and query strings (markdown image/link targets, bare URLs), leaking values into links the user might open. Fixed: a placeholder inside a URL or markdown link/image target stays redacted and the answer highlights carry a `note` entry pointing at it; prose, link display text, and bare-name handling are unchanged. Tests: `test_rehydrate_leaves_placeholder_in_image_target`, `test_rehydrate_leaves_placeholder_in_link_target`, `test_rehydrate_leaves_placeholder_in_bare_url`, `test_rehydrate_restores_prose_alongside_url`, `test_rehydrate_restores_link_display_text`, `test_rehydrate_leaves_bare_name_in_url`. | Medium | FIXED in this branch | `src/shadowfenced/redact.py` |
| 8 | DOCX headers, footers, comments, footnotes, and text boxes are not scanned (only paragraphs and table cells are extracted), so sensitive text there passes through silently. Treat as a known gap. | Medium | KNOWN LIMIT | `src/shadowfenced/extract.py` |
| 9 | Upload zip-bomb: the size cap applies to compressed bytes, so a small DOCX/PDF/ZIP can expand far past it; there is also no extraction timeout (CPU DoS via crafted files). | Medium | KNOWN LIMIT | `src/shadowfenced/extract.py` |
| 10 | Body-less cross-site POSTs to the local app (e.g. `/api/suggestions/{id}/undo`, multipart `/api/upload`) carry no CSRF protection; a malicious site can trigger them in a browser visiting it while the app runs. | Medium | KNOWN LIMIT | `src/shadowfenced/app.py` |
| 11 | The hosted judge receives the first 1,000 raw characters of a held message (`MESSAGE_JUDGE_CHARS`), i.e. raw visitor text reaches the cloud judge. Mitigated by the demo banner ("made-up data only"), still a known exposure. | Medium | KNOWN LIMIT | `src/shadowfenced/holds.py`, `src/shadowfenced/hosted.py` |
| 12 | Audio upload transcribes via the cloud even in hosted mode, skipping the hosted dictation opt-in (`SF_HOSTED_DICTATION`, default off) that gates the dictation route. | Low | KNOWN LIMIT | `src/shadowfenced/app.py` |
| 13 | The hosted per-IP rate limiter is in-memory (single process, resets on restart); it does not hold across workers or restarts. | Low | KNOWN LIMIT | `src/shadowfenced/hosted.py` |
| 14 | Extension: (a) the origin check accepts any `chrome-extension://` origin, so the pairing token is the real gate; (b) page scripts on the AI site can read the composer before redaction (inherent to the content-script model); (c) answer rehydration is not wired (`background.js` sends no mapping id, `content.js` expects `replacements` but the server returns `{text}`) — no leak, but the feature does nothing. | Low / Medium (inherent) / Info | KNOWN LIMIT | `src/shadowfenced/ext_api.py`, `extension/src/background.js`, `extension/src/content.js` |
| 15 | Reviewed and fine: decision logs (HMAC fingerprints under a per-install secret, keyless hash chain, raw values never logged); policy suggestions cannot relax protected rules (`PROTECTED_RULE_IDS` excluded from suggestions and auto-redact); streaming is buffered (single rehydrated chunk, no partial placeholder leak). | — | FINE | `src/shadowfenced/ecs_log.py`, `src/shadowfenced/suggest.py`, `src/shadowfenced/app.py` |

## Notes

- Fix 1 follows the existing `_refuse_hosted_storage` pattern (403, like the
  other hosted-disabled storage endpoints) rather than returning empty events,
  so no decision id is ever serialised in hosted mode.
- Fix 2 keeps the user-text gate (choices, file sections, WARN confirm flow)
  byte-for-byte for single-role sends; only multi-role sends take the new
  path. A WARN from a non-user role escalates to HOLD because the WARN
  confirm flow is keyed to the user gate.
- Fix 5 adds the middleware at import time when local, and the middleware
  itself also skips when hosted, so hosted keeps working either way.
- Fix 6 keeps the existing oversize payloads (unscannable + "exceeds …");
  only how much of the body is read changed.
- Fix 7 note entries reuse the highlight shape (`start`, `end`,
  `placeholder`) with an added `note` key, which the console renderer
  ignores (it only reads offsets).
