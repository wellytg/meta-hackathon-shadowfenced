# Shadowfenced browser extension (T36)

Manifest V3 guard rail for four AI chat sites — `chatgpt.com`, `claude.ai`,
`gemini.google.com`, `meta.ai` — backed by the local Shadowfenced app.
One codebase, no third-party libraries, no remote code, no analytics.
`permissions` is `storage` only; `host_permissions` is the local app plus
only the four sites.

## Layout

- `manifest_chrome.json` / `manifest_firefox.json` — per-store manifests.
- `src/logic.js` — pure, testable logic (`selectAdapter`, `spansToRanges`,
  `failSafeDecision`); loads in Node without a browser.
- `src/adapters.js` — per-site selectors for all four sites in one file.
- `src/content.js` — composer scanning overlay, hover card, send
  interception, attachment badge, reply rehydration.
- `src/background.js` — service worker / background script: pairing token
  + per-site toggles in storage, `fetch` to `http://127.0.0.1:8765` with the
  `X-Ext-Token` header, content ↔ background message bridge.
- `src/popup.html` / `src/popup.js` / `src/styles.css` — status, token, toggles.
- `build.py` — builds `dist/chrome/` + `dist/firefox/` and zips both.

## Install

Chrome/Edge: run `python extension/build.py`, then open
`chrome://extensions`, enable Developer mode, **Load unpacked** and pick
`extension/dist/chrome`.

Firefox: run `python extension/build.py`, then open `about:debugging` →
This Firefox → **Load Temporary Add-on** and pick any file inside
`extension/dist/firefox`.

## Pairing

1. Start the Shadowfenced app (it serves `http://127.0.0.1:8765`).
2. Click the extension icon → the popup probes `/api/ext/health`.
3. Paste the pairing token the app issued and press Save. It is stored in
   `chrome.storage.local` and sent as the `X-Ext-Token` header.
4. Toggle each site on/off individually in the popup.

## Coverage

- Typed and pasted text in the chat composer: debounced scan underlines
  sensitive spans; the send path (`Enter` + send button) calls
  `/api/ext/redact` first.
- `BLOCK` stops the send and shows “Blocked — not sent” + reason.
- `REDACT` replaces the editor text with `safe_text`, shows how many
  details were protected, and requires you to press send again.
- `HOLD`/`WARN` shows the reason with “Send anyway” / “Cancel”.
- File attachments are **warning only**: a badge notes files are not
  scanned; attachment bytes are never read or uploaded.
- AI replies: placeholders are restored **on-screen only** with a subtle
  highlight; nothing is written back to the site.

## Fail-safe

If the app is not running — or any check fails — the send is stopped with:

> “Shadowfenced isn't running — start it or turn the extension off”

## Limits

This is a guard rail, not a boundary. The proxy + API path is the hard
boundary; a determined sender (DevTools, another client, screenshots, or
typing into an unsupported site) bypasses the extension entirely.
