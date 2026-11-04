# Shadowfenced browser extension (T42)

Manifest V3 guard rail for five AI chat sites — `chatgpt.com`, `claude.ai`,
`gemini.google.com`, `meta.ai`, `grok.com` — backed by the local Shadowfenced app.
One codebase, no third-party libraries, no remote code, no analytics.
`permissions` is `storage` only; `host_permissions` is the local app plus
only the five sites. (`grok.com` was added Nov 4 and is not yet tested live.)

Square Windows 3.11 look everywhere: hard 2px ink outlines, paper
background, system fonts, no rounded corners. Safety orange means blocked,
steel blue means allowed, amber means review.

## Screenshots

![Popup](docs/popup.png)
*Popup: title bar, status row, pairing field, per-site switches, Test.*

![Hover card](docs/hover-card.png)
*In-page hover card: what was found, Replace with placeholder, Ignore.*

![Send-time card](docs/send-card.png)
*Send-time card: Blocked — not sent / details protected / review.*

![Test page](docs/testpage.png)
*Local test page composer with underline and card.*

## Layout

- `manifest_chrome.json` / `manifest_firefox.json` — per-store manifests.
- `src/logic.js` — pure, testable logic (`selectAdapter`, `spansToRanges`,
  `failSafeDecision`); loads in Node without a browser.
- `src/adapters.js` — the five sites plus the local demo-page adapter in
  one file (the demo adapter only matches under the dev build).
- `src/content.js` — composer scanning overlay, hover card, send
  interception, attachment badge, reply rehydration. All UI lives in one
  closed shadow root.
- `src/background.js` — service worker / background script: pairing token
  + per-site toggles in storage, `fetch` to `http://127.0.0.1:8765` with the
  `X-Ext-Token` header, content ↔ background message bridge.
- `src/popup.html` / `src/popup.js` / `src/styles.css` — title bar,
  three-state status, token Paste + Save, toggles, Test button.
- `src/icons/icon.svg` — icon source; `icon16/32/48/128.png` are generated
  by `make_icons.py` (stdlib only).
- `testpage/index.html` — local chat-composer imitation (textarea and
  contenteditable variants, send button, reply area) for demo and testing.
- `build.py` — builds `dist/chrome/` + `dist/firefox/` and zips both;
  `--dev` adds `-dev` outputs whose manifests also match the test page.

## Install

Chrome/Edge: run `python extension/build.py`, then open
`chrome://extensions`, enable Developer mode, **Load unpacked** and pick
`extension/dist/chrome`.

Firefox: run `python extension/build.py`, then open `about:debugging` →
This Firefox → **Load Temporary Add-on** and pick any file inside
`extension/dist/firefox`.

Regenerate the icons after editing `src/icons/icon.svg`:

```console
python extension/make_icons.py
```

## Pairing

1. Start the Shadowfenced app (it serves `http://127.0.0.1:8765`).
2. Click the extension icon → the popup probes `/api/ext/health`.
3. Paste the pairing token the app issued and press Save. It is stored in
   `chrome.storage.local` and sent as the `X-Ext-Token` header.
4. Toggle each site on/off individually in the popup.
5. Press **Test** to check a sample sentence through the local app.

Status row meanings: `Connected · checking locally` (steel blue),
`Not paired — paste your token` (amber), `Not running — start
Shadowfenced` (safety orange).

## Local test page (dev build only)

1. `python extension/build.py --dev` and load `extension/dist/chrome-dev`
   (or `dist/firefox-dev`) as an unpacked / temporary add-on.
2. `cd extension/testpage && python -m http.server 8000`.
3. Open `http://127.0.0.1:8000/index.html`, type something sensitive,
   hover the underline, then press send. The release manifests never
   mention this page.

## Coverage

- Typed and pasted text in the chat composer: debounced scan underlines
  sensitive spans (wavy amber for review, wavy safety orange for
  high-risk labels); the send path (`Enter` + send button) calls
  `/api/ext/redact` first. Composers may be `textarea`, `input`
  (meta.ai), or `contenteditable` elements.
- `BLOCK` stops the send and shows “Blocked — not sent” + reason.
- `REDACT` replaces the editor text with `safe_text`, shows how many
  details were protected, and requires you to press send again.
- `HOLD`/`WARN` shows the reason with “Send anyway” / “Cancel”.
- Every card has a title bar, takes keyboard focus, closes on Escape,
  parks clear of the site's send button, and auto-dismisses.
- File attachments are **warning only**: a badge notes files are not
  scanned; attachment bytes are never read or uploaded.
- AI replies: placeholders are restored **on-screen only** with a subtle
  highlight; nothing is written back to the site.

## Fail-safe

If the app is not running — or any check fails — the send is stopped with
a not-running card:

> “Shadowfenced isn't running — start it or turn the extension off”

A document-level capture guard also watches `Enter` and send-button
clicks in any editable element on a supported site, so a `BLOCK`-level
send is stopped even when no adapter attached (unknown markup after a
site update). When the app is unreachable the guard holds the send
with “Shadowfenced is not running — message not sent” and a “Send
anyway” choice — never for secret-looking text. On a supported site
with no composer found, a “Shadowfenced: not protecting this page”
badge shows and the popup paints that site row amber.

## Limits

This is a guard rail, not a boundary. The proxy + API path is the hard
boundary; a determined sender (DevTools, another client, screenshots, or
typing into an unsupported site) bypasses the extension entirely.
