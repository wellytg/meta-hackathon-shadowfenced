# Shadowfenced: prove all six (judges' guide)

Six things to check, one section each. Every case uses fictional data
only — never paste anything real. Most cases run in the **hosted demo**
(no install):

**Hosted demo:** `https://wellytg--shadowfenced-asgi.us-east.modal.run`

It shows the banner `Public demo. Use made-up data only.`

Where each case runs is marked **hosted demo**, **agent proxy**,
**extension**, or **local**.

## 1. Personal data becomes placeholders, answers come back restored

**Where: hosted demo.** Paste this into the console and send:

```text
Please send the invoice to miguel.rios@corvane.io by Friday.
```

**Expected: REDACT.** The console shows the message was sent as
`Please send the invoice to [PII_EMAIL_1] by Friday.` — the model never
sees the address. The answer arrives with the real address restored in
place of the placeholder.

## 2. Works inside ChatGPT and Gemini

**Where: extension** (needs the local app running; see
`extension/README.md`). Unzip the `v1.0-hackathon` release
(`shadowfenced-chrome.zip`), open `chrome://extensions`, enable
Developer mode, **Load unpacked**, and pick the unzipped folder. Start
the local app, click the extension icon, and paste the pairing token it
shows. Then open chatgpt.com or gemini.google.com and type:

```text
Please send the invoice to miguel.rios@corvane.io by Friday.
```

**Expected:** the address is underlined as you type; pressing send pops
a card saying details were protected and replaces the text with the
`[PII_EMAIL_1]` placeholder version — press send again to send that.
Model replies have placeholders restored on-screen only. File
attachments are warning-only in the extension.

## 3. A full identity-theft bundle is blocked in code

**Where: hosted demo.** Paste this into the console and send:

```text
New hire record: Elena Marsh, born 1988-04-12, lives at 14 Alder Street, Springfield, phone 555-013-4471.
```

**Expected: BLOCK.** Name + birth date + street address + phone proves
a full identity bundle, decided by code alone (no AI judge needed, same
on every profile). Nothing is sent; the card names the bundle.

## 4. Files are checked before upload, with a one-click redacted copy

**Where: hosted demo** (attach with the paperclip) or local. Upload
each file from `docs/samples/`:

- `bank_statement.csv` — fictional bank statement (sort codes + account
  numbers). **Expected: BLOCK (flagged)** — financial identifiers, so
  there is no "Upload anyway", only **Make a redacted copy**.
- `contacts.csv` — fictional contact list (names, emails, phones).
  **Expected: BLOCK (flagged)** — personal data only, so the card also
  offers **Upload anyway** behind a confirm, plus **Make a redacted
  copy**.
- `env_pointers.txt` — `.env`-style staging config whose values are
  pointers (`see-vault-staging-deploy-key`, `~/.aws/credentials`) and
  clearly fake strings, never key-shaped. **Expected: BLOCK
  (flagged)** — secret pointers, so no "Upload anyway", only **Make a
  redacted copy**.

Clicking **Make a redacted copy** downloads the same file with
placeholders (`[PII_EMAIL_1]`, `[SECRET_1]`, …) — the copy itself scans
CLEAN.

The hosted upload accepts `.txt`, `.md`, `.csv`, `.docx`, `.pdf` (up to
1 MB; 2 MB on a local app) and transcribes audio uploads; images need
the local app, and anything else (including a bare `.env`) is refused —
which is why the env-style sample ships with a `.txt` suffix.

## 5. Keys in any disguise, and sentences pointing at keys

**Where: hosted demo.** Two pastes.

First, a key hidden as base64 (this decodes to the same synthetic key
`setup/try_cases.py` mints at runtime from a seeded RNG — fictional):

```text
deploy is failing, the key again (base64): QUtJQTJGVjRPNjdIUFJPUklKU0U=
```

**Expected: BLOCK.** Tier 1 code decodes base64 runs and matches the key
shape inside. Nothing is sent.

Second, a sentence that points to where a key lives, without quoting one:

```text
The prod key is in ~/.aws/credentials, ask DevOps for access.
```

**Expected: HOLD.** A secret noun plus a file path is a beacon: it may
hand a thief the key, but code cannot prove it, so the message waits for
review (see "Held messages" below). With no judge it stays held — fail
closed.

## 6. Coding agents go through the OpenAI-compatible proxy

**Where: agent proxy** — `<demo>/v1/chat/completions`, no key needed.
Copy-paste this `curl` (PowerShell: one line):

```console
curl https://wellytg--shadowfenced-asgi.us-east.modal.run/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\": \"shadowfenced\", \"messages\": [{\"role\": \"user\", \"content\": \"Please send the invoice to miguel.rios@corvane.io by Friday.\"}]}"
```

**Expected: HTTP 200** with header `X-Shadowfenced-Decision: REDACT`;
the response carries a `shadowfenced` field naming the decision, and
the assistant message has the real address restored. Every role is
gated, not just user messages. A BLOCK-level prompt answers **403**
instead:

```console
curl https://wellytg--shadowfenced-asgi.us-east.modal.run/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\": \"shadowfenced\", \"messages\": [{\"role\": \"user\", \"content\": \"deploy is failing, the key again (base64): QUtJQTJGVjRPNjdIUFJPUklKU0U=\"}]}"
```

Or point any OpenAI SDK at it:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://wellytg--shadowfenced-asgi.us-east.modal.run/v1",
    api_key="not-needed",  # the demo gate takes no key
)
reply = client.chat.completions.create(
    model="shadowfenced",
    messages=[{"role": "user", "content": "Please send the invoice to miguel.rios@corvane.io by Friday."}],
)
print(reply.choices[0].message.content)
```

## Held messages

In the hosted demo, **Muse Spark** reviews held messages (a few
seconds); locally, **Muse Glimmer** does (on a box with no judge at all,
holds stay held for you — fail closed).

## Run all of them in one command

```console
python setup/try_cases.py
```

That posts 10 synthetic cases (the email, the identity bundle, the
base64 key, the pointer, and six more) to the hosted demo and prints a
table ending in `10/10 matched`. Add `--local` to check your own app on
`http://127.0.0.1:8765` instead. See `setup/quick_cases.md` for what
each row proves.
