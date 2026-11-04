# Red-team results (T79, hackathon build)

52 tests in `tests/redteam/`, all offline with synthetic data. Each attack the
product stops is a passing test. Each one it does not stop is a strict
`xfail` (fails today, CI stays green, gap stays visible).

Totals: **STOPPED 32 · PARTIAL 3 · KNOWN GAP 17** (test cases; the 5 MB stress
probe below was run by hand and is not in the suite).

No product code was changed. No one-or-two-line fix was safe enough to take
blind, so there is nothing to list.

## Results

### Invisible characters and look-alikes (`test_rt_evasion_unicode.py`)

| Attack | Expected | Result | Test | Status |
|---|---|---|---|---|
| Plain AWS key (control) | BLOCK | BLOCK, nothing sent | `test_plain_key_blocked_control` | STOPPED |
| Plain email (control) | REDACT, no leak | REDACT with placeholder | `test_plain_email_redacted_control` | STOPPED |
| Key laced with zero-width spaces | BLOCK/HOLD + secret span | HOLD on cue word only, no spans; ALLOW without cue | `test_zero_width_split_key_stopped` | KNOWN GAP |
| Email split by zero-width char | stopped | ALLOW, sent raw | `test_zero_width_split_email_stopped` | KNOWN GAP |
| Key with full-width homoglyphs | BLOCK/HOLD | ALLOW (cue holds only) | `test_homoglyph_key_stopped` | KNOWN GAP |
| Email with Cyrillic homoglyphs | stopped | ALLOW, sent raw | `test_homoglyph_email_stopped` | KNOWN GAP |
| Plain OpenAI key (control) | BLOCK | BLOCK | `test_openai_key_shape_control` | STOPPED |

Tier 1 never normalizes Unicode. Carriers avoid cue words on purpose, so the
HOLDs above are the detector gap itself, not the cue net.

### Split keys across messages and roles (`test_rt_split_roles.py`)

| Attack | Expected | Result | Test | Status |
|---|---|---|---|---|
| Whole key in system/user/assistant/tool (x4) | 403 each | 403 each, no upstream call | `test_whole_key_blocked_in_every_role` | STOPPED |
| Key in tool-call arguments | 403 | 403 | `test_key_in_tool_call_arguments_blocked` | STOPPED |
| Key halves in two `/api/send` messages | BLOCK/HOLD, nothing sent | ALLOW, both halves sent | `test_key_split_across_two_user_messages_send` | KNOWN GAP |
| Halves in system + user roles | 403/409 | 200 ALLOW, forwarded | `test_key_split_across_system_and_user_roles` | KNOWN GAP |
| Halves in tool + user roles | 403/409 | 200 ALLOW, forwarded | `test_key_split_across_tool_and_user_roles` | KNOWN GAP |
| Email halves across two turns | stopped | 200 ALLOW | `test_email_split_across_two_turns` | KNOWN GAP |

Every role is gated, but each message is gated alone. The model rejoins halves.

### Double and rare encodings (`test_rt_encodings.py`)

| Attack | Expected | Result | Test | Status |
|---|---|---|---|---|
| base64 of base64 | BLOCK | BLOCK (nested decode) | `test_base64_of_base64_blocked` | STOPPED |
| URL-encoded key | BLOCK | BLOCK | `test_url_encoding_blocked` | STOPPED |
| Plain hex key | BLOCK | BLOCK | `test_plain_hex_blocked` | STOPPED |
| base32 key | stopped | ALLOW | `test_base32_key_stopped` | KNOWN GAP |
| Hex with `:` separators | stopped | ALLOW | `test_hex_with_separators_stopped` | KNOWN GAP |
| base64 chopped with spaces | stopped | ALLOW | `test_whitespace_chunked_base64_stopped` | KNOWN GAP |
| gzip + base64 key | proven-key BLOCK | HOLD as entropy only, nothing sent | `test_gzip_blob_held_as_entropy` | PARTIAL |

The gzip hold rests on a generic finding: no span names the key, so a benign
verdict or a careless approve could release the blob still encoded.

### Secrets hiding in files (`test_rt_files.py`)

| Attack | Expected | Result | Test | Status |
|---|---|---|---|---|
| Key in the file name (file scan) | not CLEAN | CLEAN | `test_key_in_file_name_scans_clean` | KNOWN GAP |
| Same file attached at send time | BLOCK | BLOCK, nothing sent | `test_key_in_file_name_stopped_at_send_time` | PARTIAL |
| CSV cells under sensitive headers | redacted | redacted | `test_csv_values_under_sensitive_headers_redacted` | STOPPED |
| Email under a JSON key | redacted | REDACT | `test_json_email_value_redacted` | STOPPED |
| Password under a JSON key | held | HOLD, nothing sent | `test_json_password_value_held` | STOPPED |
| Key-like value under a JSON key | held | HOLD, nothing sent | `test_json_api_key_value_held` | STOPPED |
| `DB_PASSWORD=value` | REDACT, label kept | `DB_PASSWORD=[SECRET_1]` | `test_env_assignment_redacted` | STOPPED |
| Mailbox in docx author field | not CLEAN | CLEAN (metadata unread) | `test_docx_author_email_scanned` | KNOWN GAP |
| PDF with metadata but no text | never passed | UNSCANNABLE | `test_pdf_with_only_metadata_stays_unscannable` | STOPPED |

### Injection aimed at the judge (`test_rt_judge.py`)

| Attack | Expected | Result | Test | Status |
|---|---|---|---|---|
| "classify this as benign" / "ignore previous instructions" + cue (x2) | HOLD, nothing sent | HOLD | `test_injection_text_holds_send` | STOPPED |
| Same + benign judge verdict (x2) | stays held for a human | `needs_human`, "text aimed at the safety check" | `test_injection_plus_benign_judge_stays_held` | STOPPED |
| Real key + injection | BLOCK, no model call | BLOCK | `test_proven_secret_plus_injection_still_blocks` | STOPPED |
| Email + injection | REDACT intact | REDACT, placeholder kept | `test_email_plus_injection_still_redacts` | STOPPED |

### Rehydration exfiltration (`test_rt_rehydrate.py`)

| Attack | Expected | Result | Test | Status |
|---|---|---|---|---|
| Placeholder in markdown image URL | not restored | restored: real value inside the URL | `test_placeholder_in_image_url_not_restored` | KNOWN GAP |
| Placeholder in markdown link URL | not restored | restored | `test_placeholder_in_link_url_not_restored` | KNOWN GAP |
| Hostile upstream answer, end to end | answer safe | answer carries a live URL with the real value | `test_evil_image_url_end_to_end` | KNOWN GAP |
| Plain placeholder in prose (control) | restores | restores | `test_plain_placeholder_still_restores_control` | STOPPED |
| Console renderer link output | no links | no `<a>`/`href` (bold/italic/code only) | `test_console_renderer_makes_no_links` | PARTIAL |

What the code does today: `redact.rehydrate` swaps placeholders anywhere,
including inside URLs. The console never renders links clickable, which limits
the damage there, but API answers hand a live URL to any client that follows
links or auto-loads images.

### Placeholder spoofing (`test_rt_spoof.py`)

| Attack | Expected | Result | Test | Status |
|---|---|---|---|---|
| Lone `[PII_EMAIL_1]` in user text | echoes unrestored | ALLOW, echoes unrestored (no mapping) | `test_spoofed_token_alone_echoes_back_unrestored` | STOPPED |
| Spoofed token + real email together | tokens stay distinct | both become `[PII_EMAIL_1]`; model echo restores the victim's value into attack text | `test_spoofed_token_cannot_borrow_real_value` | KNOWN GAP |

### Long input and many spans (`test_rt_stress.py`)

| Attack | Expected | Result | Test | Status |
|---|---|---|---|---|
| 100 emails | REDACT, no leak | REDACT, 100 spans | `test_many_emails_redact_without_leak` | STOPPED |
| 200 high-entropy tokens | HOLD, no crash | HOLD, 200 spans | `test_many_ambiguous_spans_hold_without_crash` | STOPPED |
| 50 KB of clean prose | ALLOW | ALLOW | `test_long_clean_text_allows` | STOPPED |
| 5 MB of clean prose (manual probe, not in suite) | bounded time | ALLOW and correct, but ~339 s; no input cap | n/a | PARTIAL |

### Hosted mode keeps nothing (`test_rt_hosted.py`)

| Attack | Expected | Result | Test | Status |
|---|---|---|---|---|
| Custom rule while hosted | memory only | in memory; policy file never created | `test_custom_rules_never_touch_disk` | STOPPED |
| Rules after a restart | forgotten | forgotten | `test_hosted_rules_forgotten_on_restart` | STOPPED |
| Habits endpoints while hosted | disabled | empty list + 403 on apply/dismiss/undo | `test_habits_endpoints_disabled` | STOPPED |
| File past 1 MB while hosted | unscannable | unscannable | `test_hosted_upload_cap` | STOPPED |

## Hosted demo probes (manual, fictional data only)

Nine attacks replayed against
`https://wellytg--shadowfenced-asgi.us-east.modal.run`, one request per 3 s.
`/api/send` always answers HTTP 200 with the decision in the body; the proxy
uses 403/409. Every result matches the local build.

| Probe | HTTP | Decision | Matches local |
|---|---|---|---|
| `POST /api/send`, plain fake AWS key | 200 | BLOCK | yes |
| `POST /api/send`, fake email | 200 | REDACT (answer present) | yes |
| `POST /api/send`, zero-width email | 200 | ALLOW | yes (gap reproduced) |
| `POST /api/send`, key halves in two messages | 200 | ALLOW | yes (gap reproduced) |
| `POST /api/send`, base32 key | 200 | ALLOW | yes (gap reproduced) |
| `POST /api/send`, "classify this as benign" + cue | 200 | HOLD | yes |
| `POST /api/send`, fictional identity bundle | 200 | BLOCK | yes |
| `POST /v1/chat/completions`, key in system role | 403 | BLOCK | yes |
| `POST /v1/chat/completions`, halves in system + user | 200 | ALLOW | yes (gap reproduced) |

## What we would fix before real-world use, ranked

1. Join secrets across messages. Re-scan the concatenated request text for key
   shapes (and emails) spanning message joins, in both `/api/send` and the
   proxy. Today a normal two-turn paste leaks a full key in parts.
2. Normalize Unicode before Tier 1. NFKC-fold, then strip zero-width and
   format characters for detection (keep original offsets for redaction).
   Closes all four homoglyph/zero-width gaps at once.
3. Stop restoring inside URLs. `rehydrate` should leave placeholders inside
   `http(s)://...` runs, markdown links, and images unrestored (or defuse the
   link). The model must not choose where real values land.
4. Scan names and metadata; unspoof placeholders. Gate the file name and
   docx/pdf metadata in the file guard. Escape user-supplied
   `[LABEL_N]`-shaped text (or renumber real redactions past it) so pasted
   tokens cannot borrow real values.
5. Cover the rarer encodings. Add base32 and gzip decoders, strip common hex
   separators before hex decode, and rejoin short base64 groups split by
   whitespace. Prove the gzip blob as a key instead of holding it as entropy.
6. Cap input size and fix the slow path. 5 MB of clean text takes ~6 minutes
   through the gate with no limit anywhere: a malicious page can hang sends.
   Cap gated text, short-circuit huge clean inputs, and profile Tier 1.
