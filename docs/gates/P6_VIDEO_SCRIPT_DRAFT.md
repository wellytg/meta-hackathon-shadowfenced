# Shadowfenced — 3-minute video script (draft; target 2:50, ~150 wpm)

Numbers in {braces} come ONLY from runs/eval_<date>.json. Never typed by hand.

| # | Scene (Playwright) | Narration |
|---|---|---|
| S0 | Title card: "Shadowfenced — nothing sensitive crosses the boundary" | Every prompt is an exit. People paste passwords, customer records and internal project names into AI chats every day, and once it's sent, it's gone. Shadowfenced is a local layer that sits between people and any AI. |
| S1 | Benign question → green "Sent clean" | A normal question goes straight through. No delay, nothing changed. |
| S2 | Type config with email + canary + codename: underlines appear live; open Why | As you type, Shadowfenced underlines sensitive text, like a grammar checker for secrets. This one is a planted canary token, a fake secret the organisation seeded so any leak proves itself. Code found it, so this isn't a guess. |
| S2→ | Send → red "Blocked: proven by code", "nothing was sent upstream" | Proven secrets are blocked. Nothing leaves the machine. |
| S2b | Email + codename only → blue "Sent with 2 redactions"; open "What the AI saw"; answer re-hydrated | Personal data and internal codenames are swapped for placeholders. The AI only ever sees the placeholders, and the real values are put back locally in the answer. |
| S3 | Base64 canary in a "debug log" → BLOCK, Why says base64 | Encoding doesn't help. A base64-wrapped secret is decoded and caught by code. |
| S4 | Upload customers.csv → chip "flagged: 8 email addresses…" → send → REDACT | Files get the same check before upload. |
| S5 | Beacon sentence → purple dotted live hint → send → HOLD → "Local Muse Glimmer is reviewing…" → card: "Glimmer flagged a possible beacon (90%). Code could not prove it, so it's your call." → Send without flagged part | Some leaks aren't secrets at all. "I moved the database credentials into this file" tells a reader exactly where to look. Rules can't judge that, so a model does: Meta's Muse Glimmer, thirty billion parameters, running on this PC, so the text being checked never leaves either. But the model never blocks on its own. Code checks every claim. When code can't prove it, a person decides, with the evidence split into "proven by code" and "model suspicion only". |
| S6 | Switch provider (Meta Muse Spark ↔ local Llama) → same REDACT | It works in front of any AI: Meta's hosted Muse Spark, or anything that speaks the OpenAI API. |
| S7 | Stats panel + eval table | On our synthetic test set of {N} cases: {LEAK} leaks, a catch rate of {CATCH}, and {FALSE_ESC} false escalations on harmless look-alikes. {T1_SHARE} of inputs were settled by code alone in under a millisecond. |
| S8 | "log chain verified" badge, architecture strip, "Built Oct 27 – Nov 4 with Muse Code" | Every decision is written to a tamper-evident log. Shadowfenced: local first, proven by code, and it fails closed. The same engine can guard AI agents too. |
