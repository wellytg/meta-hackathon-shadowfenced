# Themes

Classic is the single theme. There is no theme switcher: the console and
the welcome page always render Classic, and only the theme name
(`classic`) is kept in `localStorage` under `shadowfenced.theme`.
Chat sessions, drafts, and choices are never persisted.

## Classic

The printed-collateral look the app ships with. Tokens live in
`src/shadowfenced/static/style.css` (`:root`):

| Token | Value | Use |
|---|---|---|
| `--paper` | `#f7f2e7` | canvas |
| `--paper-2` | `#fffdf6` | surfaces |
| `--ink` | `#16130e` | text, 2px outlines |
| `--muted` | `#4d463a` | secondary text |
| `--signal` | `#ff5a1f` | safety orange (secrets, block) |
| `--steel` | `#2f6b8a` | steel blue (links, allow, safe) |
| `--hold` | `#f2c230` | amber (review, warn) |

Hard offset shadows, square corners, 900-weight uppercase headlines,
monospace (`ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`)
for data (receipts, traces, chips, membrane captions), system stacks
only (no web fonts, no network).

Status mapping is shared everywhere: released/redacted -> steel,
review -> amber, blocked -> safety orange, unknown -> neutral muted.
The membrane marker (`.membrane-line`) and receipt left-edge follow the
same tokens.

Motion: `prefers-reduced-motion` collapses all membrane/receipt/CTA
transitions to instant changes. Body text keeps WCAG AA contrast
(ink/muted only for body copy).

## T32 notes

- Header, sidebar, chat list, buttons, receipts, drawer, settings menu,
  rules drawer, hold/review card, Generalise card, upload chips, and the
  welcome page share the same Classic tokens and 2px-outline treatment.
- The settings menu holds Providers, Descriptive mode, and Rules. The
  Providers item opens the (empty) `providers-panel` dialog hook.
- Layout: the app fills the window height and the page never scrolls. The
  header stays fixed on top, the composer is pinned to the bottom of the
  conversation column, only the message list (`.thread`) scrolls with
  newest-sticks-to-bottom (a "Jump to latest" button appears when the user
  scrolls up), the composer grows to ~8 lines then scrolls internally
  without moving its position, and the details drawer scrolls on its own.
- Hook points for follow-ups: `#composer-tools` next to Attach (mic
  button), the settings-menu `Providers...` item opening the empty
  `#providers-panel` dialog, and `.receipt-notes` inside each receipt.
- The welcome page is Classic throughout: the story leads with
  "You -> AI = risk", then the three-step illustration, How it works
  (four tiers), and Install in 3 steps (clone, `pip install`, run).
- Keep every existing `id`/`data-testid` — there is only one theme, so
  styles never fork markup.
- Old Fluid styles (`themes.css`, the theme picker) were removed in T32;
  `docs/results/` screenshots from earlier stages may still show them.
