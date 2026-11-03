'use strict';

/* Shadowfenced content script. Runs on the supported chat sites (plus the
 * local test page under the dev build).
 * - Finds the message box, debounces (~400ms) and asks the background
 *   worker to POST /api/ext/scan.
 * - Draws underlines in an overlay mirror inside a CLOSED shadow root
 *   positioned over the editor; never rewrites site DOM while typing.
 *   Scan hits use a wavy amber (review) line; spans whose label looks
 *   high-risk (password/secret/key/token/credential) use wavy safety
 *   orange (blocked). Lines vanish as soon as the text is fixed.
 * - Hover card: what was found + "Replace with placeholder" + "Ignore".
 * - Send-time card: "Blocked â€” not sent" / "N details protected" /
 *   "Review: reason" with "Send anyway" + "Cancel"; a separate
 *   not-running card appears when the local app is unreachable.
 * - Review-card choices (Send anyway / Cancel) are reported fields-only
 *   to POST /api/ext/choice for the "What it caught" panel.
 * - Every card lives in the closed shadow root, has a title bar, takes
 *   keyboard focus, closes on Escape, is parked clear of the site's send
 *   button, and auto-dismisses so it never covers the button permanently.
 * - Intercepts send (Enter key + send-button capture) and calls
 *   /api/ext/redact first. Fail-safe: any failure blocks the send.
 *   One send takes exactly one check (the adapter owns its button,
 *   the guard takes the rest; stale answers never send). A send
 *   shows a Checking card at once (updated every second) and waits
 *   up to 45 s for the answer; a check that never answers fails
 *   closed AND visibly with a retry card. Only a network error
 *   shows the not-running card; 401/403 shows the not-paired card.
 * - File-attachment presence shows a warning badge (warning only).
 * - Watches the AI reply area and calls /api/ext/rehydrate to restore
 *   placeholders on-screen only, with a subtle highlight. */

(function () {
  var LOGIC = (typeof globalThis !== 'undefined' && globalThis.ShadowfencedLogic) || null;
  /* Duplicate of FAILSAFE_MESSAGE in logic.js; prefer the canonical one. */
  var FAILSAFE_MESSAGE = (LOGIC && LOGIC.FAILSAFE_MESSAGE) ||
    "Shadowfenced isn't running \u2014 start it or turn the extension off";
  function toRanges(spans, len) {
    if (LOGIC && LOGIC.toRanges) return LOGIC.toRanges(spans, len);
    if (LOGIC && LOGIC.spansToRanges) return LOGIC.spansToRanges(spans, len);
    /* Tiny duplicate of spansToRanges in logic.js (see src/logic.js). */
    var out = [], i;
    var arr = Array.isArray(spans) ? spans.slice() : [];
    arr.sort(function (a, b) { return (a.start - b.start) || (a.end - b.end); });
    for (i = 0; i < arr.length; i++) {
      var s = Math.max(0, arr[i].start | 0), e = Math.min(len, arr[i].end | 0);
      if (e > s) {
        var last = out[out.length - 1];
        if (last && s <= last.end) last.end = Math.max(last.end, e);
        else out.push({ start: s, end: e });
      }
    }
    return out;
  }
  /* Map the /api/ext/redact payload ({decision, safe_text, notes}) onto
   * the internal shape ({action, reason, safe_text, count}) so a real
   * BLOCK verdict shows "Blocked — not sent" instead of collapsing
   * into the fail-safe not-running card. */
  function normalizeServerDecision(data) {
    if (LOGIC && LOGIC.normalizeRedact) return LOGIC.normalizeRedact(data);
    /* Tiny duplicate of normalizeRedact in logic.js (see src/logic.js). */
    var d = data || {};
    var action = String(d.decision || d.action || '').toUpperCase();
    if (action === 'BLOCK') {
      var b = { action: 'BLOCK', reason: d.notes || d.reason || 'Blocked by policy.' };
      if (Array.isArray(d.combination)) b.combination = d.combination.slice(0, 12);
      if (typeof d.generalised_text === 'string' && d.generalised_text) b.generalised_text = d.generalised_text;
      return b;
    }
    if (action === 'REDACT') return { action: 'REDACT', safe_text: d.safe_text || '', count: d.count || 0 };
    if (action === 'HOLD' || action === 'WARN') return { action: 'HOLD', reason: d.notes || d.reason || '' };
    if (action === 'ALLOW' || action === 'OK' || action === 'NONE') return { action: 'ALLOW' };
    return {};
  }
  function decide(res) {
    var norm = normalizeServerDecision(res && res.data);
    if (LOGIC && LOGIC.failSafeDecision) return LOGIC.failSafeDecision({ ok: !!(res && res.ok), decision: norm });
    /* Tiny duplicate of failSafeDecision in logic.js (see src/logic.js). */
    if (!res || !res.ok) return { action: 'BLOCK', reason: FAILSAFE_MESSAGE };
    if (norm.action === 'REDACT') return { action: 'REDACT', safe_text: norm.safe_text || '', count: norm.count || 0 };
    if (norm.action === 'HOLD') return { action: 'HOLD', reason: norm.reason || '' };
    if (norm.action === 'ALLOW') return { action: 'ALLOW' };
    var fb = { action: 'BLOCK', reason: norm.reason || FAILSAFE_MESSAGE };
    if (norm && Array.isArray(norm.combination)) fb.combination = norm.combination.slice(0, 12);
    if (norm && typeof norm.generalised_text === 'string' && norm.generalised_text) fb.generalised_text = norm.generalised_text;
    return fb;
  }

  /* Real attachment chips and file inputs with files count; buttons
   * (Upload & tools, Add attachment) never do. */
  function countsAsAttachment(el) {
    if (LOGIC && LOGIC.countsAsAttachment) return LOGIC.countsAsAttachment(el);
    /* Tiny duplicate of countsAsAttachment in logic.js (see src/logic.js). */
    if (!el) return false;
    var tag = String(el.tagName || '').toUpperCase();
    if (tag === 'INPUT') {
      if (String(el.type || '').toLowerCase() !== 'file') return false;
      return !!(el.files && el.files.length);
    }
    if (tag === 'BUTTON') return false;
    return true;
  }

  /* Underline tone per span: high-risk labels read as blocked (safety
   * orange), everything else as review (amber). */
  function spanTone(span) {
    var label = String((span && span.label) || '');
    if (/pass|secret|key|token|credential|ssn|card number/i.test(label)) return 'block';
    return 'review';
  }

  var DEBOUNCE_MS = 400;
  var NOTICE_TIMEOUT_MS = 15000;
  /* A send check shows a Checking card at once and waits this long for
   * the local app (whose own Glimmer timeout is the real limit) before
   * failing closed AND visibly with a retry card. */
  var SEND_CHECK_TIMEOUT_MS = 45000;
  var CHECKING_TICK_MS = 1000;
  var state = {
    adapter: null,
    editor: null,
    spans: [],        // raw spans from last scan: {start,end,label,placeholder}
    ignored: [],      // ignored ranges {start,end}
    redactedArmed: false, // set after REDACT replace; next send goes through
    sending: false,
    siteOn: true,
    sendSeq: 0, // epoch: every send intent takes the next number
    sendTimer: null, // timeout arming the in-flight check
    sendTickTimer: null, // elapsed-seconds updater for the Checking card
    sendCheckStarted: 0, // Date.now() when the in-flight check began
    sendAnchor: null, // where the in-flight check's cards park
    sendGuard: false, // true when the in-flight check is the capture guard's
    sendText: '', // text the in-flight guard check is holding
    sendCardSeq: 0 // epoch of the send-time card on screen (0 = none)
  };

  var adaptersNS = (typeof globalThis !== 'undefined' && globalThis.ShadowfencedAdapters) || null;
  function pickAdapter() {
    var host = location.hostname || '';
    if (adaptersNS && adaptersNS.selectAdapter) return adaptersNS.selectAdapter(host);
    return null;
  }
  function qs(selectors, root) {
    if (adaptersNS && adaptersNS.queryFirst) return adaptersNS.queryFirst(selectors, root);
    var scope = root || document;
    for (var i = 0; i < selectors.length; i++) {
      try {
        var el = scope.querySelector(selectors[i]);
        if (el) return el;
      } catch (e) { /* bad selector */ }
    }
    return null;
  }

  function editorText(el) {
    if (!el) return '';
    if ('value' in el && typeof el.value === 'string') return el.value;
    return el.innerText || el.textContent || '';
  }
  function setEditorText(el, text) {
    if ('value' in el && typeof el.value === 'string') {
      el.value = text;
      el.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      el.focus();
      document.execCommand('selectAll', false, null);
      document.execCommand('insertText', false, text);
    }
  }

  /* ---------- messaging ---------- */
  function sendToBg(msg) {
    return new Promise(function (resolve) {
      try {
        chrome.runtime.sendMessage(msg, function (res) {
          if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
          else resolve(res || { ok: false, error: 'empty response' });
        });
      } catch (e) {
        resolve({ ok: false, error: String(e && e.message || e) });
      }
    });
  }

  /* ---------- closed shadow root: overlay + cards + badge ---------- */
  var overlayHost = null, overlayShadow = null;
  var mirrorBox = null, mirrorInner = null;
  var card = null, cardBar = null, cardTitle = null, cardClose = null;
  var cardBody = null, cardBtns = null;
  var badge = null, unprot = null;
  var cardMode = null; // 'hover' | 'notice' | null
  var cardAnchor = null; // last anchor rect for repositioning notices
  var hideTimer = null, noticeTimer = null;

  var SHADOW_CSS =
    '.sf-box{position:absolute;display:none;overflow:hidden;pointer-events:none;}' +
    '.sf-mirror{width:100%;height:100%;overflow:hidden;color:transparent;pointer-events:none;' +
    'white-space:pre-wrap;word-wrap:break-word;box-sizing:border-box;}' +
    '.sf-mirror u{pointer-events:auto;cursor:help;' +
    'text-decoration-thickness:2px;text-underline-offset:3px;}' +
    '.sf-mirror u[data-tone="review"]{text-decoration:underline wavy #b97f00;background:rgba(185,127,0,.10);}' +
    '.sf-mirror u[data-tone="block"]{text-decoration:underline wavy #d9480f;background:rgba(217,72,15,.12);}' +
    '.sf-card{position:absolute;display:none;min-width:220px;max-width:300px;' +
    'pointer-events:auto;background:#f4f1e8;color:#1a1a1a;border:2px solid #1a1a1a;' +
    'box-shadow:4px 4px 0 rgba(0,0,0,.35);font:13px/1.45 "Segoe UI",system-ui,Tahoma,Verdana,sans-serif;}' +
    '.sf-bar{display:flex;align-items:center;justify-content:space-between;gap:8px;' +
    'padding:4px 4px 4px 8px;font-weight:700;border-bottom:2px solid #1a1a1a;}' +
    '.sf-bar[data-tone="block"]{background:#d9480f;color:#fff;}' +
    '.sf-bar[data-tone="review"]{background:#e8a800;color:#1a1a1a;}' +
    '.sf-bar[data-tone="allow"]{background:#2f6f9f;color:#fff;}' +
    '.sf-x{flex:none;cursor:pointer;border:2px solid currentColor;background:transparent;color:inherit;' +
    'font:inherit;line-height:1;padding:0 6px;}' +
    '.sf-cbody{padding:8px;}' +
    '.sf-cbtns{display:flex;gap:8px;padding:0 8px 8px;}' +
    '.sf-cbtns button{flex:1;cursor:pointer;color:#1a1a1a;background:#e8e2d2;border:2px solid #1a1a1a;' +
    'box-shadow:inset -2px -2px 0 #a39d8a,inset 2px 2px 0 #fffdf6;padding:6px 8px;font:inherit;}' +
    '.sf-cbtns button:active{box-shadow:inset 2px 2px 0 #a39d8a;}' +
    '.sf-badge{position:absolute;display:none;pointer-events:auto;max-width:70vw;' +
    'background:#f7e6ad;color:#1a1a1a;border:2px solid #1a1a1a;padding:4px 8px;' +
    'font:12px/1.4 "Segoe UI",system-ui,Tahoma,Verdana,sans-serif;}' +
    '.sf-unprot{position:absolute;right:12px;bottom:12px;display:none;max-width:70vw;' +
    'background:#f7e6ad;color:#1a1a1a;border:2px solid #1a1a1a;padding:4px 8px;' +
    'font:12px/1.4 "Segoe UI",system-ui,Tahoma,Verdana,sans-serif;}';

  function ensureOverlay() {
    if (overlayHost) return;
    overlayHost = document.createElement('div');
    overlayHost.setAttribute('data-sf', 'overlay');
    overlayHost.style.cssText = 'position:fixed;left:0;top:0;width:100vw;height:100vh;' +
      'z-index:2147483646;pointer-events:none;margin:0;padding:0;';
    document.documentElement.appendChild(overlayHost);
    overlayShadow = overlayHost.attachShadow({ mode: 'closed' });
    var style = document.createElement('style');
    style.textContent = SHADOW_CSS;
    overlayShadow.appendChild(style);
    mirrorBox = document.createElement('div');
    mirrorBox.className = 'sf-box';
    mirrorInner = document.createElement('div');
    mirrorInner.className = 'sf-mirror';
    mirrorInner.setAttribute('aria-hidden', 'true');
    mirrorBox.appendChild(mirrorInner);
    overlayShadow.appendChild(mirrorBox);
    badge = document.createElement('div');
    badge.className = 'sf-badge';
    badge.setAttribute('role', 'status');
    badge.textContent = '\u26a0 Attachment present \u2014 files are not scanned, only this warning is shown.';
    overlayShadow.appendChild(badge);
    unprot = document.createElement('div');
    unprot.className = 'sf-unprot';
    unprot.setAttribute('role', 'status');
    unprot.textContent = (LOGIC && LOGIC.NOT_PROTECTING_TEXT) || 'Shadowfenced: not protecting this page';
    overlayShadow.appendChild(unprot);
    card = document.createElement('div');
    card.className = 'sf-card';
    card.setAttribute('tabindex', '-1');
    card.setAttribute('role', 'dialog');
    cardBar = document.createElement('div');
    cardBar.className = 'sf-bar';
    cardBar.setAttribute('data-tone', 'review');
    cardTitle = document.createElement('span');
    cardClose = document.createElement('button');
    cardClose.className = 'sf-x';
    cardClose.setAttribute('type', 'button');
    cardClose.setAttribute('aria-label', 'Close');
    cardClose.textContent = '\u00d7';
    cardClose.addEventListener('click', hideCard);
    cardBar.appendChild(cardTitle);
    cardBar.appendChild(cardClose);
    cardBody = document.createElement('div');
    cardBody.className = 'sf-cbody';
    cardBtns = document.createElement('div');
    cardBtns.className = 'sf-cbtns';
    card.appendChild(cardBar);
    card.appendChild(cardBody);
    card.appendChild(cardBtns);
    card.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { ev.stopPropagation(); hideCard(); }
    });
    overlayShadow.appendChild(card);
  }

  /* Copy the editor's text metrics so the mirror lines up exactly. */
  function syncMirrorStyle() {
    if (!state.editor || !mirrorInner) return;
    var cs = getComputedStyle(state.editor);
    var props = ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'lineHeight',
      'letterSpacing', 'wordSpacing', 'textTransform', 'textIndent', 'textAlign',
      'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
      'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth'];
    for (var i = 0; i < props.length; i++) {
      try { mirrorInner.style[props[i]] = cs[props[i]]; } catch (e) { /* ignore */ }
    }
    mirrorInner.style.borderStyle = 'solid';
    mirrorInner.style.borderColor = 'transparent';
    var ws = cs.whiteSpace;
    mirrorInner.style.whiteSpace = (ws && ws.indexOf('pre') === 0) ? ws : 'pre-wrap';
  }

  function syncOverlay() {
    ensureOverlay();
    if (!state.editor || !document.contains(state.editor)) {
      mirrorBox.style.display = 'none';
      return;
    }
    var r = state.editor.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) { mirrorBox.style.display = 'none'; return; }
    mirrorBox.style.display = 'block';
    mirrorBox.style.left = r.left + 'px';
    mirrorBox.style.top = r.top + 'px';
    mirrorBox.style.width = r.width + 'px';
    mirrorBox.style.height = r.height + 'px';
    syncMirrorStyle();
    try {
      mirrorInner.scrollTop = state.editor.scrollTop || 0;
      mirrorInner.scrollLeft = state.editor.scrollLeft || 0;
    } catch (e) { /* ignore */ }
  }

  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function renderOverlay() {
    ensureOverlay();
    if (!state.editor || !state.spans.length) {
      mirrorBox.style.display = 'none';
      return;
    }
    var text = editorText(state.editor);
    var ranges = toRanges(state.spans.map(function (s) {
      return { start: s.start, end: s.end };
    }), text.length);
    syncOverlay();
    var html = '', pos = 0, k;
    var sorted = ranges.slice().sort(function (a, b) { return a.start - b.start; });
    for (k = 0; k < sorted.length; k++) {
      var r = sorted[k];
      if (r.start < pos) continue;
      var isIgnored = state.ignored.some(function (g) { return r.start >= g.start && r.end <= g.end; });
      html += escHtml(text.slice(pos, r.start));
      if (isIgnored) {
        html += escHtml(text.slice(r.start, r.end));
      } else {
        var meta = null;
        for (var m = 0; m < state.spans.length; m++) {
          var sp = state.spans[m];
          if (sp.start < r.end && sp.end > r.start) { meta = sp; break; }
        }
        var idx = meta ? state.spans.indexOf(meta) : -1;
        var tone = meta ? spanTone(meta) : 'review';
        html += '<u tabindex="0" data-sf-idx="' + idx + '" data-tone="' + tone + '">' +
          escHtml(text.slice(r.start, r.end)) + '</u>';
      }
      pos = r.end;
    }
    html += escHtml(text.slice(pos));
    mirrorInner.innerHTML = html;
    var underlines = mirrorInner.querySelectorAll('u');
    underlines.forEach(function (u) {
      var open = function () {
        var idx = Number(u.getAttribute('data-sf-idx'));
        showHoverCard(u, state.spans[idx]);
      };
      u.addEventListener('mouseenter', open);
      u.addEventListener('focus', open);
      u.addEventListener('mouseleave', scheduleHideCard);
      u.addEventListener('blur', scheduleHideCard);
      u.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape') { ev.stopPropagation(); hideCard(); }
        else if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); }
      });
    });
  }

  /* ---------- cards (all inside the closed shadow root) ---------- */
  function sendButtonRect() {
    try {
      var btn = state.adapter ? qs(state.adapter.sendButton, document) : null;
      if (btn && document.contains(btn)) return btn.getBoundingClientRect();
    } catch (e) { /* ignore */ }
    return null;
  }

  function rectsOverlap(a, b) {
    return a && b && a.left < b.right && a.right > b.left &&
      a.top < b.bottom && a.bottom > b.top;
  }

  function placeCard(anchorRect) {
    // Measure first, then park the card clear of the send button.
    card.style.visibility = 'hidden';
    card.style.display = 'block';
    var w = card.offsetWidth || 260, h = card.offsetHeight || 120;
    var x = Math.max(8, Math.min(window.innerWidth - w - 8, anchorRect.left));
    var y = anchorRect.top - h - 8;
    if (y < 8) y = anchorRect.bottom + 8;
    var btn = sendButtonRect();
    var guess = { left: x, right: x + w, top: y, bottom: y + h };
    if (rectsOverlap(guess, btn)) {
      var below = anchorRect.bottom + 8;
      if (!rectsOverlap({ left: x, right: x + w, top: below, bottom: below + h }, btn)) {
        y = below;
      } else {
        x = Math.max(8, (btn.left || 0) - w - 8);
        guess = { left: x, right: x + w, top: y, bottom: y + h };
        if (rectsOverlap(guess, btn)) y = (btn.bottom || 0) + 8;
      }
    }
    y = Math.max(8, Math.min(window.innerHeight - h - 8, y));
    card.style.left = x + 'px';
    card.style.top = y + 'px';
    card.style.visibility = '';
  }

  function showCard(opts) {
    ensureOverlay();
    /* One send = one card: a stale send answer never replaces a newer
     * send's card. Hover cards carry no epoch and never win this test. */
    if (opts.mode !== 'hover' && state.sendCardSeq > (opts.sendSeq || 0)) return;
    hideCard(true);
    cardMode = opts.mode || 'notice';
    if (cardMode === 'hover') state.sendCardSeq = 0;
    else state.sendCardSeq = opts.sendSeq || 0;
    cardAnchor = opts.anchor || null;
    cardBar.setAttribute('data-tone', opts.tone || 'review');
    cardTitle.textContent = opts.title || 'Shadowfenced';
    card.setAttribute('aria-label', opts.title || 'Shadowfenced');
    cardBody.innerHTML = '';
    if (opts.bodyEl) cardBody.appendChild(opts.bodyEl);
    else cardBody.textContent = opts.body || '';
    if (Array.isArray(opts.chips) && opts.chips.length) {
      var chipsBox = document.createElement('div');
      opts.chips.forEach(function (label) {
        var chip = document.createElement('span');
        chip.textContent = label;
        chipsBox.appendChild(chip);
      });
      cardBody.appendChild(chipsBox);
    }
    cardBtns.innerHTML = '';
    (opts.buttons || []).forEach(function (def) {
      var b = document.createElement('button');
      b.setAttribute('type', 'button');
      b.textContent = def.label;
      b.addEventListener('click', function () {
        if (def.keepOpen !== true) hideCard();
        if (def.onClick) { def.onClick(); } else if (def.label === 'Cancel' && opts.title === 'Review before sending') { reportExtChoice('cancel'); }
      });
      cardBtns.appendChild(b);
    });
    cardBtns.style.display = (opts.buttons && opts.buttons.length) ? '' : 'none';
    var anchor = cardAnchor || { left: window.innerWidth / 2 - 130, top: window.innerHeight / 2, bottom: window.innerHeight / 2 };
    placeCard(anchor);
    card.style.display = 'block';
    /* The hover card never takes focus: stealing it from the composer
     * swallows keys typed after the card opens (the second half of the
     * message never reaches the editor). Notice cards still take focus
     * so Escape and their buttons work at once. */
    var wantFocus = (opts.mode !== 'hover');
    if (LOGIC && LOGIC.shouldFocusCard) {
      try { wantFocus = LOGIC.shouldFocusCard(opts.mode); } catch (e2) { /* keep default */ }
    }
    if (wantFocus) { try { card.focus(); } catch (e) { /* ignore */ } }
    if (cardMode === 'notice') {
      if (noticeTimer) clearTimeout(noticeTimer);
      noticeTimer = setTimeout(hideCard, NOTICE_TIMEOUT_MS);
    }
  }

  function hideCard(silent) {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    if (noticeTimer && !silent) { clearTimeout(noticeTimer); noticeTimer = null; }
    else if (noticeTimer && silent) { clearTimeout(noticeTimer); noticeTimer = null; }
    if (!card || card.style.display === 'none') { cardMode = null; state.sendCardSeq = 0; return; }
    var stealBack = false;
    try { stealBack = card.contains(document.activeElement); } catch (e) { stealBack = false; }
    card.style.display = 'none';
    cardMode = null;
    state.sendCardSeq = 0;
    cardAnchor = null;
    if (stealBack && state.editor && document.contains(state.editor)) {
      try { state.editor.focus(); } catch (e2) { /* ignore */ }
    }
  }

  function scheduleHideCard() {
    if (cardMode !== 'hover' || state.sendCardSeq !== 0) return;
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(function () { if (cardMode === 'hover') hideCard(); }, 300);
  }

  function showHoverCard(anchorEl, span) {
    /* The send card wins over hover: never open one while a send check
     * is in flight or a send-time card is up. */
    if (!span || cardMode === 'notice' || state.sendCardSeq !== 0 || state.sending) return;
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    var label = span.label || 'sensitive detail';
    var text = editorText(state.editor);
    var match = '';
    try { match = text.slice(Math.max(0, span.start), Math.max(0, span.end)); } catch (e) { match = ''; }
    if (match.length > 80) match = match.slice(0, 80) + '\u2026';
    var body = document.createElement('div');
    var what = document.createElement('div');
    what.textContent = match ? ('"' + match + '" looks sensitive.') : 'This text looks sensitive.';
    var sub = document.createElement('div');
    sub.textContent = span.placeholder ? ('Suggested placeholder: ' + span.placeholder) : '';
    body.appendChild(what);
    if (span.placeholder) body.appendChild(sub);
    // Hover card follows the underline, so anchor on its rect.
    var r = anchorEl.getBoundingClientRect();
    cardMode = 'hover';
    showCard({
      mode: 'hover',
      tone: spanTone(span),
      title: 'Found: ' + label,
      bodyEl: body,
      anchor: r,
      buttons: [
        {
          label: 'Replace with placeholder',
          onClick: function () { replaceSpanWithPlaceholder(span); }
        },
        {
          label: 'Ignore',
          onClick: function () {
            state.ignored.push({ start: span.start, end: span.end });
            renderOverlay();
          }
        }
      ]
    });
    cardMode = 'hover';
    // Hovering the card itself keeps it open.
    card.onmouseenter = function () {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    };
    card.onmouseleave = function () { scheduleHideCard(); };
  }

  function replaceSpanWithPlaceholder(span) {
    if (!state.editor || !span || !span.placeholder) return;
    var text = editorText(state.editor);
    var next = text.slice(0, span.start) + span.placeholder + text.slice(span.end);
    setEditorText(state.editor, next); // local edit only; user still presses send
    state.spans = [];
    renderOverlay();
    scheduleScan();
  }

  /* ---------- attachment badge (warning only, inside shadow root) ---------- */
  function updateAttachBadge() {
    ensureOverlay();
    if (!state.adapter || !state.editor || !document.contains(state.editor)) {
      badge.style.display = 'none';
      return;
    }
    var found = null;
    for (var i = 0; i < state.adapter.attachment.length; i++) {
      try {
        var inputs = document.querySelectorAll(state.adapter.attachment[i]);
        for (var k = 0; k < inputs.length; k++) {
          if (countsAsAttachment(inputs[k])) { found = inputs[k]; break; }
        }
      } catch (e) { /* ignore */ }
      if (found) break;
    }
    if (found) {
      var r = state.editor.getBoundingClientRect();
      badge.style.display = 'block';
      badge.style.left = Math.max(8, r.left) + 'px';
      badge.style.top = Math.max(8, r.top - 34) + 'px';
    } else {
      badge.style.display = 'none';
    }
  }

  /* Fail-visible badge: on a supported site with no composer attached
   * (or a broken adapter after a site update), say so on the page and
   * report it so the popup paints that site row amber. */
  var lastProtReport = null;
  function reportProtection(siteId, protecting) {
    var key = siteId + ':' + (protecting ? 'on' : 'off');
    if (key === lastProtReport) return;
    lastProtReport = key;
    sendToBg({ type: 'SF_PROT', site: siteId, protecting: protecting });
  }
  function updateUnprotBadge(show) {
    ensureOverlay();
    if (!unprot) return;
    unprot.style.display = show ? 'block' : 'none';
  }

  /* ---------- scanning ---------- */
  var debounceTimer = null;
  function scheduleScan() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(runScan, DEBOUNCE_MS);
  }
  function runScan() {
    if (!state.editor || !state.siteOn) return;
    var text = editorText(state.editor);
    if (!text.trim()) { state.spans = []; renderOverlay(); return; }
    sendToBg({ type: 'SF_SCAN', text: text, host: location.hostname }).then(function (res) {
      if (!res || !res.ok) return; // scan failures stay silent; send-time check is fail-safe
      var spans = (res.data && res.data.spans) || [];
      state.spans = spans.filter(function (s) {
        return !state.ignored.some(function (g) { return s.start >= g.start && s.end <= g.end; });
      });
      renderOverlay();
    });
  }

  /* ---------- send interception ---------- */
  function editorAnchor() {
    try {
      if (state.editor && document.contains(state.editor)) return state.editor.getBoundingClientRect();
    } catch (e) { /* ignore */ }
    return { left: window.innerWidth / 2 - 130, top: 200, bottom: 200, right: window.innerWidth / 2 + 130 };
  }

  /* The Checking card: shown at once on every send (Classic look,
   * amber), updated every second with the elapsed wait, replaced by
   * the result card when the answer arrives. */
  function checkingBodyText(elapsed) {
    return 'Checking with Shadowfenced\u2026 ' + elapsed + 's.' +
      ' Nothing is sent until the answer arrives.';
  }
  function showCheckingCard(seq, anchor) {
    showCard({
      mode: 'checking',
      tone: 'review',
      title: 'Checking\u2026',
      body: checkingBodyText(0),
      anchor: anchor || editorAnchor(),
      buttons: [],
      sendSeq: seq
    });
  }
  function updateCheckingCard(seq) {
    if (state.sendCardSeq !== seq || !state.sending) return;
    if (cardMode !== 'checking') return;
    var elapsed = Math.floor((Date.now() - state.sendCheckStarted) / 1000);
    try { cardBody.textContent = checkingBodyText(elapsed); } catch (e) { /* ignore */ }
  }
  /* Failure cards from logic.js (or the tiny fallback below): a 45 s
   * timeout offers Retry, 401/403 says not paired, anything else
   * says the app is not running. */
  function failureCardFor(input) {
    if (LOGIC && LOGIC.sendFailureCard) {
      try { return LOGIC.sendFailureCard(input); } catch (e) { /* fall through */ }
    }
    if ((input || {}).timeout) {
      return { tone: 'review', title: 'Still no answer',
        body: 'Still no answer \u2014 nothing was sent.',
        buttons: ['retry', 'cancel'] };
    }
    if (input && (input.status === 401 || input.status === 403)) {
      return { tone: 'block', title: 'Not paired',
        body: 'Not paired \u2014 open the Shadowfenced popup and paste the pairing token.' +
        ' Nothing was sent.',
        buttons: ['ok'] };
    }
    return { tone: 'block', title: "Shadowfenced isn't running",
      body: FAILSAFE_MESSAGE + ' Nothing was sent.', buttons: ['ok'] };
  }
  function showFailureCard(card, seq, onRetry) {
    var btns = (card.buttons || ['ok']).map(function (b) {
      if (b === 'retry') return { label: 'Retry', onClick: function () { onRetry(); } };
      if (b === 'cancel') return { label: 'Cancel' };
      return { label: 'OK' };
    });
    showCard({
      tone: card.tone,
      title: card.title,
      body: card.body,
      anchor: state.sendAnchor || editorAnchor(),
      buttons: btns,
      sendSeq: seq
    });
  }

  var BLOCK_KIND_LABELS = { name: 'name', dob: 'date of birth', age: 'age', street: 'street address', postcode: 'postcode', city: 'city', employer: 'employer', jobtitle: 'job title', school: 'school', gender: 'gender', medical: 'medical details', plate: 'vehicle plate', vehicle: 'vehicle', phone: 'phone number', email: 'email address', dated_event: 'dates' };
  /* Chips for the BLOCK card are label-only spans rendered by showCard. */
  /* Send-verdict cards without side effects: BLOCK, HOLD and REDACT
   * only. Stale answers refresh at most; they never send or rewrite. */
  function showVerdictCard(d, seq) {
    if (d.action === 'REDACT') {
      var n = d.count || 0;
      showCard({
        tone: 'allow',
        title: 'Shadowfenced',
        body: n + ' detail' + (n === 1 ? '' : 's') +
          ' protected \u2014 press send again to send the protected text.',
        anchor: editorAnchor(),
        buttons: [{ label: 'OK' }],
        sendSeq: seq
      });
      return;
    }
    if (d.action === 'HOLD') {
      showCard({
        tone: 'review',
        title: 'Review before sending',
        body: d.reason || 'Please review before sending.',
        anchor: editorAnchor(),
        buttons: [
          { label: 'Send anyway', onClick: function () { reportExtChoice('send_anyway'); state.redactedArmed = true; doSend(); } },
          { label: 'Cancel' }
        ],
        sendSeq: seq
      });
      return;
    }
    // BLOCK (including fail-safe when the app is unreachable)
    var reason = d.reason || FAILSAFE_MESSAGE;
    if (reason === FAILSAFE_MESSAGE) {
      showCard({
        tone: 'block',
        title: "Shadowfenced isn't running",
        body: 'Start the Shadowfenced app, then press send again. ' +
          'Nothing was sent. ' + reason,
        anchor: editorAnchor(),
        buttons: [{ label: 'OK' }],
        sendSeq: seq
      });
      return;
    }
    showCard({
      tone: 'block',
      title: 'Blocked \u2014 not sent',
      body: reason,
      chips: (Array.isArray(d.combination) ? d.combination : []).map(function (k) {
        return BLOCK_KIND_LABELS[String(k)] || String(k).replace(/_/g, ' ');
      }),
      anchor: editorAnchor(),
      buttons: [{ label: 'OK' }],
      sendSeq: seq
    });
  }

  /* Report one HOLD card choice fields-only to /api/ext/choice
   * (T69). Uses the last verdict's decision id, kinds and rule ids;
   * without a verdict there is nothing to attribute, so nothing sends. */
  function reportExtChoice(choice) {
    var ctx = state.lastChoice || null;
    if (!ctx || !ctx.decision_id) return;
    var payload = null;
    if (LOGIC && LOGIC.buildChoicePayload) {
      try {
        payload = LOGIC.buildChoicePayload({
          decision_id: ctx.decision_id, choice: choice,
          kinds: ctx.kinds || [], rule_ids: ctx.rule_ids || [],
          site: (location.hostname || ''), source: 'text'
        });
      } catch (e) { payload = null; }
    }
    if (!payload) return;
    sendToBg({
      type: 'SF_CHOICE',
      decision_id: payload.decision_id, choice: payload.choice,
      kinds: payload.kinds, rule_ids: payload.rule_ids,
      site: payload.site, source: payload.source
    });
  }
  function handleRedactResult(res, seq, allowSend) {
    if (!res || !res.ok) {
      var status = (res && typeof res.status === 'number') ? res.status : null;
      if (status === 401 || status === 403) {
        showFailureCard(failureCardFor({ status: status }), seq, function () {
          requestSendCheck();
        });
        return;
      }
      showVerdictCard(decide(res), seq);
      return;
    }
    var raw = (res && res.data) || {};
    state.lastChoice = {
      decision_id: String(raw.decision_id || raw.id || ''),
      kinds: Array.isArray(raw.kinds) ? raw.kinds : [],
      rule_ids: Array.isArray(raw.rule_ids) ? raw.rule_ids : []
    };
    var d = decide(res);
    if (d.action === 'ALLOW') {
      if (!allowSend) return; // stale answer: never send on its own
      state.redactedArmed = true; // scanned clean / app allowed: let this send through
      doSend();
      return;
    }
    if (d.action === 'REDACT') {
      if (!allowSend) { showVerdictCard(d, seq); return; }
      setEditorText(state.editor, d.safe_text);
      state.spans = [];
      renderOverlay();
      state.redactedArmed = true; // user must press send again to send the safe text
      showVerdictCard(d, seq);
      return;
    }
    showVerdictCard(d, seq);
  }

  function handleLateRedact(res, seq) {
    // A timed-out or superseded check answered late: refresh the card
    // at most, never send or rewrite the composer.
    handleRedactResult(res, seq, false);
  }

  /* One send = one check = one card. Every send intent takes the next
   * epoch; an answer from an older epoch only refreshes a card, never
   * sends. */
  function beginSendCheck() {
    state.lastChoice = null; // a new send needs a fresh verdict before choices count
    state.sending = true;
    var seq = ++state.sendSeq;
    if (state.sendTimer) clearTimeout(state.sendTimer);
    if (state.sendTickTimer) clearInterval(state.sendTickTimer);
    state.sendCheckStarted = Date.now();
    state.sendTimer = setTimeout(function () { onSendTimeout(seq); }, SEND_CHECK_TIMEOUT_MS);
    state.sendTickTimer = setInterval(function () { updateCheckingCard(seq); }, CHECKING_TICK_MS);
    return seq;
  }
  function endSendCheck(seq) {
    if (seq !== state.sendSeq) return false; // stale answer: inform only
    if (state.sendTimer) { clearTimeout(state.sendTimer); state.sendTimer = null; }
    if (state.sendTickTimer) { clearInterval(state.sendTickTimer); state.sendTickTimer = null; }
    state.sending = false;
    return true;
  }
  function onSendTimeout(seq) {
    state.sendTimer = null;
    if (state.sendTickTimer) { clearInterval(state.sendTickTimer); state.sendTickTimer = null; }
    if (seq !== state.sendSeq || !state.sending) return;
    // Fail closed AND visibly: the check never answered, so hold the
    // send with a retry card instead of hanging silently. The gate is
    // released so Retry (or pressing send again) retries; a late answer
    // still replaces the card but never sends on its own.
    state.sending = false;
    var anchor = state.sendAnchor || editorAnchor();
    if (state.sendGuard) {
      showGuardCard(guardCard({ ok: false, timeout: true, text: state.sendText || '' }), anchor, seq);
      return;
    }
    showFailureCard(failureCardFor({ timeout: true }), seq, function () {
      requestSendCheck();
    });
  }

  function requestSendCheck() {
    if (state.sending) return; // a check is in flight; the caller held the event
    if (state.redactedArmed) { state.redactedArmed = false; return; } // user confirmed; let it go
    if (!state.siteOn) return; // extension off for this site: do not interfere
    var seq = beginSendCheck();
    state.sendGuard = false;
    state.sendAnchor = editorAnchor();
    showCheckingCard(seq, state.sendAnchor);
    var text = editorText(state.editor);
    sendToBg({ type: 'SF_REDACT', text: text, host: location.hostname }).then(function (res) {
      if (!endSendCheck(seq)) { handleLateRedact(res, seq); return; }
      handleRedactResult(res, seq, true);
    });
  }

  function doSend() {
    // Re-dispatch the user's intent after a successful check.
    var btn = state.adapter ? qs(state.adapter.sendButton, document) : null;
    if (btn) { btn.click(); return; }
    if (state.editor) {
      state.editor.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
    }
  }

  function bindSendInterception() {
    document.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' || ev.shiftKey || ev.isComposing) return;
      if (!state.editor || !state.editor.contains(ev.target) ||
          (ev.target !== state.editor && !state.editor.contains(ev.target))) {
        // Only intercept Enter inside the composer.
        if (ev.target !== state.editor) return;
      }
      if (!state.siteOn) return; // extension off for this site: do not interfere
      if (state.redactedArmed) { state.redactedArmed = false; return; } // confirmed; let this send through once
      // A send intent while a check runs is held, never passed through.
      ev.preventDefault();
      ev.stopPropagation();
      requestSendCheck();
    }, true);
    document.addEventListener('click', function (ev) {
      if (!state.adapter || !state.siteOn) return; // not ours, or off: do not interfere
      if (state.redactedArmed) { state.redactedArmed = false; return; } // confirmed; let this send through once
      var t = ev.target;
      if (!(t instanceof Element)) return;
      var btn = qs(state.adapter.sendButton, document);
      if (btn && (t === btn || btn.contains(t))) {
        // Held while a check runs: one send = one check, nothing unchecked.
        ev.preventDefault();
        ev.stopPropagation();
        requestSendCheck();
      }
    }, true);
  }

  /* ---------- document-level capture guard (fail visible, fail closed) ----------
   * Runs even when no adapter attached (unknown composer markup after a
   * site update): on Enter (no Shift) or a click on a send-style button
   * while focus sits in any editable element on a supported host, the
   * same /api/ext/redact check runs and a BLOCK-level send is stopped.
   * When the app is unreachable the send is held with a not-running
   * card offering "Send anyway" — except for secret-looking text, which
   * is never offered an override. Non-BLOCK verdicts let the send
   * through exactly once (re-armed so the re-dispatched event passes). */
  function isEditable(el) {
    if (LOGIC && LOGIC.isEditableElement) return LOGIC.isEditableElement(el);
    /* Tiny duplicate of isEditableElement in logic.js (see src/logic.js). */
    if (!el) return false;
    var tag = String(el.tagName || '').toUpperCase();
    if (tag === 'TEXTAREA') return true;
    if (tag === 'INPUT') {
      var t = String(el.type || 'text').toLowerCase();
      return (t !== 'file' && t !== 'hidden' && t !== 'checkbox' && t !== 'radio' &&
        t !== 'button' && t !== 'submit' && t !== 'image' && t !== 'reset');
    }
    try { if (el.isContentEditable) return true; } catch (e) { /* ignore */ }
    if (el.getAttribute) {
      var ce = null;
      try { ce = el.getAttribute('contenteditable'); } catch (e2) { ce = null; }
      if (ce !== null && ce !== undefined && String(ce).toLowerCase() !== 'false') return true;
      try { if (el.getAttribute('role') === 'textbox') return true; } catch (e3) { /* ignore */ }
    }
    return false;
  }
  function shouldGuard(o) {
    if (LOGIC && LOGIC.guardShouldIntercept) return LOGIC.guardShouldIntercept(o);
    /* Tiny duplicate of guardShouldIntercept in logic.js (see src/logic.js). */
    if (!o || !o.supported || o.adapterAttached || o.armed || o.sending || !o.siteOn) return false;
    if (o.kind === 'enter') {
      if (o.key !== 'Enter' || o.shift || o.composing) return false;
      return !!o.activeEditable;
    }
    if (o.kind === 'click') return !!(o.onSendButton && o.activeEditable);
    return false;
  }
  function guardCard(input) {
    if (LOGIC && LOGIC.captureGuardCard) return LOGIC.captureGuardCard(input);
    /* Tiny duplicate of captureGuardCard in logic.js (see src/logic.js). */
    if ((input || {}).timeout) {
      return {
        send: false, tone: 'review', title: 'Still no answer',
        body: 'Still no answer \u2014 nothing was sent.', buttons: ['retry', 'cancel']
      };
    }
    if (input && (input.status === 401 || input.status === 403)) {
      return {
        send: false, tone: 'block', title: 'Not paired',
        body: 'Not paired \u2014 open the Shadowfenced popup and paste the pairing token.' +
        ' Nothing was sent.',
        buttons: ['ok']
      };
    }
    if (!input || !input.ok) {
      return {
        send: false, tone: 'block', title: "Shadowfenced isn't running",
        body: FAILSAFE_MESSAGE + ' Nothing was sent.', buttons: ['ok']
      };
    }
    var d = input.decision || {};
    if (d.action === 'BLOCK') {
      return {
        send: false, tone: 'block', title: 'Blocked \u2014 not sent',
        body: d.reason || 'Blocked by policy.', buttons: ['ok']
      };
    }
    return { send: true };
  }
  function findSendButton(el) {
    try {
      if (el && el.closest) {
        return el.closest('button[type="submit"], button[aria-label*="Send" i], [aria-label*="Send" i]');
      }
    } catch (e) { /* detached node */ }
    return null;
  }
  function adapterOwns(el) {
    return !!(state.editor && el && (el === state.editor || state.editor.contains(el)));
  }
  function adapterSendButton() {
    try {
      if (!state.adapter) return null;
      var btn = qs(state.adapter.sendButton, document);
      return (btn && document.contains(btn)) ? btn : null;
    } catch (e) { return null; }
  }
  /* The adapter path owns clicks on its own send button (and anything
   * inside it): the guard stays out so one click means one check. */
  function adapterSendButtonOwns(el) {
    var btn = adapterSendButton();
    if (!btn || !el) return false;
    if (el === btn) return true;
    try { return !!(el instanceof Element && btn.contains(el)); } catch (e) { return false; }
  }
  function guardAnchor(el) {
    try {
      if (el && el.getBoundingClientRect) {
        var r = el.getBoundingClientRect();
        if (r && (r.width || r.height)) return r;
      }
    } catch (e) { /* ignore */ }
    return editorAnchor();
  }
  var pendingGuardResend = null; // {kind, el} for the held-then-confirmed send
  function resendGuarded() {
    var p = pendingGuardResend;
    pendingGuardResend = null;
    if (!p || !p.el || !document.contains(p.el)) return;
    // state.redactedArmed is set by the caller; the owning interceptor
    // consumes the pass while the other stays out (single ownership).
    if (p.kind === 'click') {
      try { p.el.click(); } catch (e) { /* ignore */ }
    } else {
      try {
        p.el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
      } catch (e) { /* ignore */ }
    }
  }
  function showGuardCard(card, anchorRect, seq) {
    var anchor = anchorRect || editorAnchor();
    if (card.buttons.indexOf('retry') !== -1) {
      showCard({
        tone: card.tone, title: card.title, body: card.body, anchor: anchor,
        buttons: [
          { label: 'Retry', onClick: function () {
            runCaptureGuard(state.sendText || '', state.sendAnchor || editorAnchor(), pendingGuardResend);
          } },
          { label: 'Cancel' }
        ],
        sendSeq: seq
      });
      return;
    }
    if (card.buttons.indexOf('send-anyway') !== -1) {
      showCard({
        tone: card.tone, title: card.title, body: card.body, anchor: anchor,
        buttons: [
          { label: 'Send anyway', onClick: function () { state.redactedArmed = true; resendGuarded(); } },
          { label: 'Cancel' }
        ],
        sendSeq: seq
      });
      return;
    }
    showCard({
      tone: card.tone, title: card.title, body: card.body, anchor: anchor,
      buttons: [{ label: 'OK' }],
      sendSeq: seq
    });
  }
  function runCaptureGuard(text, anchorRect, resend) {
    if (state.sending) return;
    var seq = beginSendCheck();
    state.sendGuard = true;
    state.sendAnchor = anchorRect;
    state.sendText = text;
    showCheckingCard(seq, anchorRect);
    pendingGuardResend = resend;
    sendToBg({ type: 'SF_REDACT', text: text, host: location.hostname }).then(function (res) {
      if (!endSendCheck(seq)) {
        // Late answer: a BLOCK still refreshes the card, anything else
        // stays quiet, and nothing ever sends on its own.
        var late = guardCard({ ok: !!(res && res.ok), status: res && res.status, decision: normalizeServerDecision(res && res.data), text: text });
        if (!late.send) showGuardCard(late, anchorRect, seq);
        return;
      }
      var card = guardCard({ ok: !!(res && res.ok), status: res && res.status, decision: normalizeServerDecision(res && res.data), text: text });
      if (card.send) {
        // Not a BLOCK verdict: let this one send through exactly once.
        state.redactedArmed = true;
        resendGuarded();
        return;
      }
      showGuardCard(card, anchorRect, seq);
    });
  }
  function activeEditable() {
    var active = null;
    try { active = document.activeElement; } catch (e) { active = null; }
    return isEditable(active) ? active : null;
  }
  function bindCaptureGuard() {
    document.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' || ev.shiftKey || ev.isComposing) return;
      if (state.redactedArmed) { state.redactedArmed = false; return; } // confirmed; let it go
      var active = null;
      try { active = document.activeElement; } catch (e) { active = null; }
      if (adapterOwns(ev.target) || adapterOwns(active)) return; // adapter path owns it
      if (!shouldGuard({
        kind: 'enter', key: ev.key, shift: !!ev.shiftKey, composing: !!ev.isComposing,
        supported: !!pickAdapter(), adapterAttached: false, armed: false,
        sending: state.sending, siteOn: state.siteOn,
        activeEditable: !!activeEditable(), onSendButton: false
      })) return;
      ev.preventDefault();
      ev.stopPropagation();
      var ed = activeEditable();
      runCaptureGuard(editorText(ed), guardAnchor(ed), { kind: 'enter', el: ed });
    }, true);
    document.addEventListener('click', function (ev) {
      var t = ev.target;
      if (!(t instanceof Element)) return;
      if (adapterOwns(t)) return;
      if (adapterSendButtonOwns(t)) return; // the adapter path owns its send button
      var btn = findSendButton(t);
      if (!btn) return;
      if (state.redactedArmed) { state.redactedArmed = false; return; } // confirmed; let it go
      var focused = activeEditable() || (isEditable(t) ? t : null);
      if (!shouldGuard({
        kind: 'click', supported: !!pickAdapter(), adapterAttached: false, armed: false,
        sending: state.sending, siteOn: state.siteOn,
        activeEditable: !!focused, onSendButton: true
      })) return;
      ev.preventDefault();
      ev.stopPropagation();
      runCaptureGuard(editorText(focused), guardAnchor(focused || btn), { kind: 'click', el: btn });
    }, true);
  }

  /* ---------- reply rehydration (on-screen only) ---------- */
  var rehydrated = new WeakSet();
  function watchReplies() {
    if (!state.adapter) return;
    var areas = [];
    state.adapter.replyArea.forEach(function (sel) {
      try {
        document.querySelectorAll(sel).forEach(function (el) { areas.push(el); });
      } catch (e) { /* ignore */ }
    });
    var target = areas.length ? areas[0] : document.body;
    var pending = false;
    new MutationObserver(function () {
      if (pending) return;
      pending = true;
      setTimeout(function () {
        pending = false;
        rehydrateVisible();
      }, 800);
    }).observe(target, { childList: true, subtree: true, characterData: true });
  }

  function collectTextNodes(root) {
    var out = [];
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var node;
    while ((node = walker.nextNode())) {
      if (node.nodeValue && node.nodeValue.indexOf('[') !== -1) out.push(node);
    }
    return out;
  }

  function rehydrateVisible() {
    if (!state.siteOn) return;
    var scopes = [];
    state.adapter.replyArea.forEach(function (sel) {
      try {
        document.querySelectorAll(sel).forEach(function (el) {
          if (!rehydrated.has(el)) { rehydrated.add(el); scopes.push(el); }
        });
      } catch (e) { /* ignore */ }
    });
    if (!scopes.length) return;
    var texts = scopes.map(function (el) { return el.innerText || ''; }).join('\n');
    if (texts.indexOf('[') === -1) return; // no placeholders on screen
    sendToBg({ type: 'SF_REHYDRATE', text: texts, host: location.hostname }).then(function (res) {
      if (!res || !res.ok || !res.data || !res.data.replacements) return;
      var reps = res.data.replacements; // [{placeholder, value}]
      scopes.forEach(function (scope) {
        collectTextNodes(scope).forEach(function (node) {
          var v = node.nodeValue, changed = false;
          reps.forEach(function (r) {
            if (r.placeholder && r.value && v.indexOf(r.placeholder) !== -1) {
              v = v.split(r.placeholder).join(r.value);
              changed = true;
            }
          });
          if (changed) {
            var frag = document.createElement('span');
            frag.textContent = v;
            frag.setAttribute('data-sf', 'rehydrated');
            frag.style.cssText = 'background:rgba(185,127,0,.15);border-bottom:2px dotted #b97f00;';
            node.parentNode.replaceChild(frag, node);
          }
        });
      });
    });
  }

  /* ---------- boot ---------- */
  function attachToEditor(editor) {
    if (editor === state.editor) return;
    state.editor = editor;
    state.ignored = [];
    state.spans = [];
    editor.addEventListener('input', function () { syncOverlay(); scheduleScan(); });
    editor.addEventListener('scroll', syncOverlay);
    scheduleScan();
    renderOverlay();
    updateAttachBadge();
  }

  function tick() {
    var adapter = pickAdapter();
    if (!adapter) return;
    if (!state.adapter || state.adapter.id !== adapter.id) {
      state.adapter = adapter;
      state.editor = null;
      watchReplies();
    }
    sendToBg({ type: 'SF_GET_STATE', host: location.hostname }).then(function (res) {
      state.siteOn = !(res && res.data && res.data.disabled);
    });
    var editor = qs(adapter.editor, document);
    if (editor && editor !== state.editor) attachToEditor(editor);
    if (state.adapter) {
      reportProtection(state.adapter.id, !!state.editor);
      updateUnprotBadge(!state.editor && state.siteOn);
    }
    if (state.editor && !document.contains(state.editor)) {
      state.editor = null;
      if (mirrorBox) mirrorBox.style.display = 'none';
    }
    if (state.editor) {
      syncOverlay();
      updateAttachBadge();
      if ((cardMode === 'notice' || cardMode === 'checking') && cardAnchor) {
        // Follow the composer; hover cards hide instead of chasing.
        cardAnchor = editorAnchor();
        placeCard(cardAnchor);
      }
    }
  }

  bindSendInterception();
  bindCaptureGuard();
  setInterval(tick, 1500);
  tick();
  window.addEventListener('resize', function () {
    syncOverlay();
    if (cardMode === 'hover') hideCard();
    else if ((cardMode === 'notice' || cardMode === 'checking') && cardAnchor) {
      cardAnchor = editorAnchor();
      placeCard(cardAnchor);
    }
  });
  // Scrolling moves the composer: keep the mirror glued, park hover away.
  window.addEventListener('scroll', function () {
    syncOverlay();
    if (cardMode === 'hover') hideCard();
    else if ((cardMode === 'notice' || cardMode === 'checking') && cardAnchor) {
      cardAnchor = editorAnchor();
      placeCard(cardAnchor);
    }
  }, true);
})();
