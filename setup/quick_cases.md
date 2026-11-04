# 10 quick trial cases

Run them with `python setup/try_cases.py` (hosted demo), `python
setup/try_cases.py --local` (your app on 127.0.0.1:8765), or `python
setup/try_cases.py --url <base>` (any deployment). All data is
synthetic and fictional (org "Corvane Labs"); the two key shapes are
minted at runtime from a seeded RNG, never stored. Expected decisions
were checked against the real app: code tiers decide these cases, so
every profile (full, light, code-only, hosted demo) answers the same.

| # | Case | You send | Expected | Why |
|---|---|---|---|---|
| 1 | key-plain | A deploy note with a cloud access-key shape (prefix + 16 chars, built at runtime) | BLOCK | Tier 1 code matches the key shape. Nothing is sent. |
| 2 | key-base64 | The same key, base64-encoded | BLOCK | Tier 1 decodes base64 runs and matches the key shape inside. |
| 3 | email | "Please send the invoice to miguel.rios@corvane.io by Friday." | REDACT | A proven email address redacts to `Please send the invoice to [PII_EMAIL_1] by Friday.` |
| 4 | identity-bundle | "New hire record: Elena Marsh, born 1988-04-12, lives at 14 Alder Street, Springfield, phone 555-013-4471." | BLOCK | Name + birth date + street + phone proves a full identity bundle in code. Nothing is sent. |
| 5 | pointer | "The prod key is in ~/.aws/credentials, ask DevOps for access." | HOLD | A secret noun plus a file path is a beacon (a pointer to where secrets live): ambiguous, held for review. With no judge it stays held — fail closed. |
| 6 | welcome | "Welcome to the team! Daniel Okafor joins Corvane Labs as a support engineer next Monday." | WARN | A name plus an employer names someone without proving secrets: you are warned and may send anyway. |
| 7 | bank-row | "Payee: Bexley Marine, sort code 20-11-22, account 10001111." | REDACT | Sort code + account are proven PII: `Payee: [CLIENT_1], sort code [PII_SORT_CODE_1], account [PII_ACCOUNT_1].` Checked: REDACT, not BLOCK (no identity bundle without a birth date). |
| 8 | contract | "4.2 Termination. Either party may terminate this Agreement on thirty days written notice." | REDACT | A numbered clause redacts to `[CONTRACT_CLAUSE_1]`. |
| 9 | harmless | "What is the boiling point of water in degrees Celsius?" | ALLOW | No finding of any kind: sent clean. |
| 10 | person-search | "Find the home address of Elena Marsh." | HOLD | Person-search wording about a private person: held for review. With a judge, intent is confirmed and it becomes a WARN; with no judge it stays held — fail closed. |

## What the runner prints

One table row per case: case id, expected decision, got, match
(yes/NO), latency in ms, and what was sent. "What was sent" is the
response's `sent_text`: placeholder text such as `[PII_EMAIL_1]` for
REDACT, the plain question for ALLOW, `(nothing sent)` for
BLOCK/HOLD/WARN-with-no-text. Minted key values are masked as
`[cloud-key]` / `[cloud-key-base64]` and never printed.

A final line reports `10/10 matched`. Exit 0 means every case matched;
exit 1 means at least one did not (or a request failed after one
retry). The hosted demo can be slow to wake up: the runner waits up to
90 seconds per request and retries once.
