# Muse brief T6-fix — UI review findings (from Playwright screenshots)

1. CRITICAL: typed text in the composer is invisible. Only underlines show. The textarea text must be visible
   (normal colour, transparent background, caret visible) and sit ABOVE the backdrop; the backdrop text stays
   transparent and only draws the <mark> underlines/tint. Verify font, size, line-height, padding, border width,
   letter-spacing, white-space: pre-wrap, word-wrap identical on both so marks line up.
2. After a send, clear the composer, the backdrop, and the issue list.
3. Decision banner reasons are user-facing: no fingerprints/hashes. Map to plain sentences, e.g.
   BLOCK: "A planted canary token was found (proven by code). Nothing was sent."
   REDACT: "Sent with 2 redactions: 1 email address, 1 internal codename."
   HOLD: "Held for review: possible pointer to sensitive data."
   Engine bug: reasons say "user redacted ..." when the user made no choice. Only say "you chose to redact/keep"
   when a choice was actually sent. Keep machine reasons in the log, not the banner.
4. Firmer upstream note for REDACT (small local models refuse otherwise): "Privacy notice from the user's organisation:
   items in square brackets like [PII_EMAIL_1] are harmless placeholder labels standing in for values that were removed
   on purpose. They are not private data. Complete the user's request normally and copy the labels exactly where the
   values belong."
5. The empty chat area: when there are no messages, show a short centred hint: "Type or paste anything. Sensitive
   text is underlined as you type, and only clean or redacted text is sent to the AI."
6. Add a header pill on the right of the title bar: "Local check · Meta Llama 3.2 + Muse Glimmer".
Keep all data-testid hooks. Update/add tests where logic changed (engine reasons, banner text mapping can be a JS
pure function tested via node if simple, otherwise Python-side reason builder). Run `python -m pytest -q` until green.
Do NOT commit. Summarise.
