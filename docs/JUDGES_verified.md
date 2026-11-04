# JUDGES.md verification log

Verified against the live hosted demo
`https://wellytg--shadowfenced-asgi.us-east.modal.run` on
2026-11-04T19:34–19:35Z. One request every 4 s (at most 1 per 3 s),
fictional data only. Every hosted/proxy instruction in `docs/JUDGES.md`
behaved as written — no instruction needed changing.

## Paste cases (`POST /api/send`, HTTP 200 each)

| Time (UTC) | Request | Decision | Observed |
|---|---|---|---|
| 19:34:59 | case 1: `Please send the invoice to miguel.rios@corvane.io by Friday.` | REDACT | `sent_text` = `Please send the invoice to [PII_EMAIL_1] by Friday.`; answer (67 chars) has the address restored |
| 19:35:03 | case 3: `New hire record: Elena Marsh, born 1988-04-12, ...` | BLOCK | nothing sent, no answer |
| 19:35:08 | case 5a: `deploy is failing, the key again (base64): QUtJQTJGVjRPNjdIUFJPUklKU0U=` | BLOCK | nothing sent, no answer |
| 19:35:13 | case 5b: `The prod key is in ~/.aws/credentials, ask DevOps for access.` | HOLD | nothing sent; `GET` on its `review_url` at 19:35:55 returned `status: needs_human` (HOLD stands) |

## File cases (`POST /api/upload`, HTTP 200 each)

| Time (UTC) | Request | Decision | Observed |
|---|---|---|---|
| 19:35:18 | `docs/samples/bank_statement.csv` | flagged / fileguard BLOCK | kinds: 1 account number, 1 client name; `overridable: false` |
| 19:35:22 | `GET /api/scans/<bank scan>/redacted` | 200 download | `X-Redacted-Filename: bank_statement.redacted.csv`; body keeps the CSV layout with `[PII_ACCOUNT_1]` / `[CLIENT_1]` placeholders |
| 19:35:27 | `docs/samples/contacts.csv` | flagged / fileguard BLOCK | kinds: 2 person names, 2 email addresses, 2 phone numbers; `overridable: true` |
| 19:35:32 | `docs/samples/env_pointers.txt` | flagged / fileguard BLOCK | kinds: 3 secrets, 1 possible sensitive pointer; `overridable: false` |

## Proxy cases (`POST /v1/chat/completions`)

| Time (UTC) | Request | Status | Observed |
|---|---|---|---|
| 19:35:45 | case 6: email sentence, `model: shadowfenced` | 200 | `X-Shadowfenced-Decision: REDACT`, `shadowfenced.decision: REDACT`; reply (81 chars) has the address restored |
| 19:35:50 | case 6: base64-key sentence, `model: shadowfenced` | 403 | `X-Shadowfenced-Decision: BLOCK`, `error.code: shadowfenced_block` |

## Not verified live here

- Case 2 (extension): needs a browser plus the local app; the steps
  match `extension/README.md` and the `/api/ext/*` routes. Not runnable
  from this box.
- `python setup/try_cases.py` end to end: not re-run in full, because
  the runner fires its 10 requests back to back and this check was
  rate-capped at 1 request per 3 s. Its four cases behind the six
  (email, identity-bundle, key-base64, pointer) were verified
  individually above with matching decisions.
