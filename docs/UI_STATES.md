# UI states (T38b)

Single source of truth for what every interactive element looks like and
does in each state. Style is fixed: Windows 3.11 / DOS — square corners
(`border-radius: 0` everywhere), hard 2px outlines, paper background, ink
text, safety orange for blocked, steel blue for allowed, amber for review.
No state is ever colour alone: every state carries a text label. Behaviour
follows the common pattern of ChatGPT, Claude, Gemini and Grok: Enter
sends, Shift+Enter makes a newline, Send is disabled while empty, the
microphone shows recording then puts text in the box, the newest message
stays in view, and errors appear next to the thing that failed.

Global rules that apply to every element below:

- Keyboard: Tab reaches every control in DOM order (header → chat list →
  thread → composer row → drawer). Focus is always visible (3px steel
  outline). A skip link jumps to the conversation.
- Escape closes the topmost panel first: dictation notice → rules drawer
  → providers panel → settings menu → phone chat/details panels. Escape
  never deletes typed text.
- Inline errors are one line inside the composer (`#composer-error`,
  `role="status"`), next to the control that failed, and clear on the
  next action (typing, sending, attaching).
- `prefers-reduced-motion` disables all animation and transforms.
- All `id` and `data-testid` hooks are frozen; tests pin them.

## 1. Composer (`textarea#composer`, `data-testid="composer"`)

| State | User sees | User can do |
|---|---|---|
| Idle, empty | Placeholder "Type a message — sensitive text gets underlined…", Send disabled | Type, paste, attach, dictate |
| Typing | Text in ink; sensitive spans underlined (red secret, amber pii/business, dotted steel-blue suspicion) within ~250 ms; issue cards appear | Keep typing; Redact/Keep/Why per span |
| Focus | 3px steel outline | Same as typing |
| Busy (send in flight) | "Checking locally…" next to Send (after 2 s: "Muse Glimmer is checking on this PC…"); box stays editable but Send is disabled | Wait; Jump to latest still works |
| Success | Box cleared (the server took the text) | Type the next message |
| Error (send failed) | Text KEPT; one-line `#composer-error`: "Not sent — couldn't reach the checker. Your text was kept; try again." | Edit and press Send again |
| Disabled | Never (always editable) | — |

Enter sends; Shift+Enter and Alt+Enter insert a newline; composition
(IME) Enter is ignored until the character commits. A second Enter while
busy is dropped (one request in flight).

## 2. Send (`button#send`, `data-testid="send"`)

| State | User sees | User can do |
|---|---|---|
| Disabled (empty, no files) | Greyed "Send" (55% opacity) | Nothing (click is a no-op guard) |
| Enabled (text or files) | Ink button with orange offset shadow | Click or Enter to send |
| Busy | Disabled + "Checking locally…" label beside it (after 2 s: "Muse Glimmer is checking on this PC…") | Nothing until the check returns |
| Hover/focus | Steel background / steel outline | Same as enabled |

## 3. Attach (`button#paperclip`, `data-testid="attach"`, `input#file-input`, chips)

| State | User sees | User can do |
|---|---|---|
| Idle | Square "+" button, bottom-left inside the composer box | Open the file picker (.txt/.md/.csv/.docx/.pdf/.wav/.mp3/.m4a/.ogg/.webm) |
| Attached clean/flagged | Chip with kind letter, name and finding count; amber edge when flagged | Expand findings, remove with "x" |
| Upload failed | NO thread banner; one-line composer error "Could not attach NAME: reason" | Pick another file |
| Disabled | Never (works while busy; files queue for the next send) | — |

## 4. Microphone (`button[data-testid="mic-btn"]` in `#composer-tools`)

| State | User sees | User can do |
|---|---|---|
| Idle | Square mic button, bottom-right inside the composer box | Start (first time: consent dialog first) |
| Consent (first run only) | Dialog: voice goes to Meta for transcription, only the flag is stored; focused "Turn on dictation" | Turn on, or Cancel (Escape closes, focus returns) |
| No mic / denied | One-line status: "No microphone found…" / "Microphone permission denied…" | Dismiss by next action; retry |
| Listening | Status line "Listening 0:07 — press Stop to finish."; button becomes "Stop 0:07" ticking each second; auto-stops at 60 s | Click Stop (the same button) to finish |
| Transcribing | Status "Transcribing…"; button disabled with "…" | Wait (a few seconds) |
| Success | Transcript inserted AT THE CURSOR (selection replaced); `input` fires so underlines appear within ~250 ms; nothing auto-sends | Edit the text, then Send |
| Error | One-line status: "Dictation failed. Please try again." (or the server reason); typed text untouched | Retry |

## 5. Model picker (`select#provider`, `data-testid="provider-select"`)

| State | User sees | User can do |
|---|---|---|
| Idle | "Model:" + current model ("Muse Spark · cloud"); unavailable options carry "(no API key)" and are disabled | Open the list |
| Saving | Picker disabled while `POST /api/provider` runs | Wait |
| Saved | Picker enabled; header "Model: …" line updates to the picked model | Continue |
| Save failed | Picker re-enabled; server keeps the old default (no error thrown at the user; choice stays local) | Retry |

## 6. Provider cards (`#providers-panel`, `data-testid="provider-card-ID"`)

| State | User sees | User can do |
|---|---|---|
| Closed | Panel hidden; "Providers…" button in Settings | Open |
| Open, idle | Notice "Your key stays on this computer…", one card per provider with Connected/Not connected badge + storage line | Paste key, Save, Test connection, Disconnect, Log in with OpenRouter |
| Busy | Clicked button disabled; Test shows "Testing…" | Wait; other cards stay usable |
| Success | "Saved." / provider message / "Disconnected."; panel re-renders with fresh badges | Continue |
| Error | Card-local message: server reason or "Could not save the key." / "Test failed." / "Could not disconnect."; button re-enabled | Fix and retry |
| Escape | Panel hides, focus returns to "Providers…" | — |

## 7. Settings menu (`details#settings-menu`)

Rows: Providers (`#providers-open`), Descriptive mode (relocated switch),
Rules (relocated button), Developer mode (`data-testid="developer-mode"`,
disabled, "coming soon"). Closed: "Settings" summary. Open: panel with the
rows; Escape or re-click closes. Disabled rows stay visible but inert so
users learn they exist.

## 8. Rules drawer (`[data-testid="sf-drawer"]`, role=dialog)

| State | User sees | User can do |
|---|---|---|
| Closed | Nothing | Open via Rules (focus moves to Close) |
| Loading | Empty lists, then built-in (locked, "built-in" tag) + custom lists render | Wait |
| Ready | Kind/value/action form + Add; custom rows have Remove | Add (validates: value required, bad pattern rejected with message) or Remove |
| Error | `sf-error` line with the server reason ("add failed (400)" etc.); drawer stays open, form kept | Fix and retry |
| Escape/backdrop | Drawer closes, focus returns to Rules | — |

## 9. Descriptive mode (`[data-testid="sf-descriptive-toggle"]`)

Toggle button "Show more detail: on/off" (`aria-pressed`, persisted in
`localStorage`). Off: trace timelines hidden. On: every receipt is
followed by its "Decision trace (N steps)" list and the details drawer
shows the per-turn trace. Switching repaints instantly, never re-sends.

## 10. Receipts and Why (`details[data-testid="receipt"]`, `data-decision`)

One compact receipt per turn: icon + TEXT LABEL + "Why" disclosure. The
icon and the thin left rule carry colour; the label always carries words.

| Decision | Label | Left rule |
|---|---|---|
| ALLOW | "Sent" | steel blue |
| REDACT | "N sensitive details protected" | steel blue |
| HOLD | "Review required — nothing sent" | amber |
| BLOCK | "Blocked — not sent" | safety orange |
| UNKNOWN | "Not sent — couldn't check" | muted grey |

Collapsed: summary line only. Expanded (Why): server `user_message`,
"resolved at tier N", "trace: step > step" — never raw values, ids or
hashes. Unknown is neutral: never "sent". Receipts are clickable: selecting
one loads the turn into the details drawer.

## 11. Hold / review card (`[data-testid="evidence-card"]`)

| State | User sees | User can do |
|---|---|---|
| Reviewing | Spinner + "Local Muse Glimmer (30B, on this PC) is reviewing… Ns"; two columns (Proven by code / Model suspicion only); redacted preview | Wait, or (after timeout) choose |
| Needs human | "Glimmer could not clear this — your call." + suspicion lines with confidence | "Send without flagged part" or "Don't send" |
| Timed out (>180 polls) | "Review timed out. Decide below." + both buttons | Choose |
| Approved/sent | Receipt flips to REDACT/ALLOW with the server line; answer bubble appears | Continue |
| Denied | Receipt stays HOLD: "Denied: nothing was sent." | Continue |
| Choice failed | Buttons re-enable; "Something went wrong; try again." | Retry |

Buttons disable the moment one is clicked (no double ruling). A failed
poll retries silently; the spinner keeps counting.

## 12. Generalise and Send-anyway choices

- Person-search WARN: note in the receipt + "Generalise"
  (`data-testid="reid-generalise"`) fills the composer with the generalised
  text, posts a "Generalised. Your real values stay on this PC." note with
  an Undo that restores the exact original draft. Nothing sends by itself.
- HOLD approve IS the send-anyway path ("Send without flagged part"); deny
  ("Don't send") ends the turn. Both are explicit clicks, never automatic.

## 13. Details drawer (`details#details-drawer`, `data-testid` hooks inside)

Open on wide screens; toggled by "Details" on narrow ones. Empty: "Select
a turn to see details." Selected turn: Decision, Reasons, Protected
(placeholder labels like `[PII_EMAIL_1]`, never values), Model, Timing, and
the descriptive trace when enabled. Content never includes raw text.

## 14. Log viewer (`#log-view`, `#log-events`, `#log-badge`)

| State | User sees | User can do |
|---|---|---|
| Idle | Badge "log chain verified · N events" (or "log chain broken · N events" in orange) + "View log" | Open |
| Loading | Button disabled, "Loading…" | Wait |
| Open | Newest-first list: time · decision · tier · reasons · fingerprints (12-hex); empty log says "No decisions logged yet."; broken chain says so | Read; "Hide log" collapses |
| Failed | One row: "Could not load the log." | Retry |

## 15. Pairing token (`#ext-token`, Show/Copy/Regenerate)

Hidden by default ("••••••••"). Show toggles to "Hide" and back. Copy
writes the token to the clipboard (no-op without a token). Regenerate
disables itself while `POST /api/ext/pairing/regenerate` runs; success:
"New token ready. Update the extension."; failure (including non-local
callers, 403): "Could not regenerate the token." Old token stops working
immediately after a successful regenerate.

## 16. Chat list, New chat, Jump to latest

- Chat list (`ul#chat-list`): one button per chat, current marked
  `aria-current`; clicking saves the draft, swaps the thread and restores
  that chat's draft. Sessions live in page memory only — never stored,
  never sent. Empty: first send titles the chat from its first 42 chars.
- New chat (`button#new-chat`): saves the draft, clears thread/composer/
  membrane, focuses the composer.
- Jump to latest (`button#jump-latest`): hidden while the thread bottom is
  visible; appears on scroll-up ("Jump to latest ↓"); click sticks to the
  bottom again and focuses the composer. New turns auto-scroll ONLY while
  stuck; a scrolled-up user is never yanked down.

## Mapping to code

Composer/Send/Attach/thread/jump: `static/app.js` + `#composer-block`,
`#checking` ("Checking locally…"), `#composer-error`, `#jump-latest`.
Mic: `static/dictation.js` + `.sf-dictation-msg` (12px, single line).
Model/providers: `static/app.js` (`/api/guard`, `/api/providers`,
`/api/provider`) + `static/providers.js`. Rules/descriptive:
`static/rules.js`. States under test: `window.ShadowfencedUI`
(`receiptLabel`, `receiptIcon`, `membraneFor`, `isSendEnabled`),
`window.ShadowfencedDictation.formatElapsed`; static pins in
`tests/test_t38b_states.py`. Risk card: `riskCardModel` in
`window.ShadowfencedUI` (see §17).

## 17. Risk card (T45)

One component for every risky turn, built from
`riskCardModel(decisionResponse)` (null for a clean ALLOW, three rows
otherwise). Clean ALLOW shows no card, only the quiet receipt
"✓ Checked · nothing sensitive" (small, steel blue, "Why" on click).
REDACT/WARN/HOLD/BLOCK show the card attached to the message: title bar
with the decision word, rows Sending (placeholders highlighted) /
Why it's risky (one plain grouped sentence per finding) / What was
done (Replaced with placeholders / Blocked — not sent / Waiting for
you: Generalise · Send anyway · Cancel). Compact by default (one line
per row; "Show details" expands the full trace); single column at
390px. HOLD review renders the same card (`evidence-card` hook kept)
with its live review section inside.
