# Chat UI patterns (generic synthesis)

Researched via a web-enabled subagent survey of widely used AI chat apps
(public pages, help-centre screenshots and design write-ups). This note
records the common structural pattern only, in our own words. No logos,
icon artwork or brand names are reproduced here or in the app.

## The common pattern

- App frame: a left navigation panel (chat list, new chat) plus a main
  chat pane. The panel collapses on narrow screens.
- Slim top header: a sidebar toggle on one end, the conversation title
  or model picker in the middle, and new-chat / settings controls on
  the other end.
- Centred conversation column: a narrow readable measure (about
  700-800px), vertically scrolling, with whitespace around it.
- Message list: user turns aligned to one side, assistant answers to
  the other, newest at the bottom with auto-stick; a "jump to latest"
  control appears when scrolled up.
- Composer: one rounded box pinned to the bottom of the conversation
  column, same width as the column. The text field grows upward a few
  lines, then scrolls internally.
- Tool slots inside the composer: an attach control in the bottom-left
  corner, voice-input and send controls in the bottom-right corner.
- Send affordance: Enter sends, Shift+Enter makes a new line, and Send
  stays disabled while the box is empty.
- Status feedback lives inline: recording / error hints appear as a
  small line inside the composer, not as a separate block.
- Phone layout: the conversation and composer take the full screen;
  the chat list and any inspector become slide-out panels opened from
  header buttons. Nothing stacks above or below the composer.

## How we apply it (Classic style)

Paper background, ink text, hard 2px outlines and offset shadows.
Safety orange marks blocked, steel blue marks safe, amber marks review.
System fonts only, monospace for data. Reduced-motion preferences are
respected. All existing `id` and `data-testid` hooks are preserved.
