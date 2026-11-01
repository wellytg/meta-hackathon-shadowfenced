'use strict';

/* Shadowfenced content script. Runs on the four supported chat sites.
 * - Finds the message box, debounces (~400ms) and asks the background
 *   worker to POST /api/ext/scan.
 * - Draws underlines in an overlay layer inside a CLOSED shadow root
 *   positioned over the editor; never rewrites site DOM while typing.
 * - Hover card: what was found + "Replace with placeholder" + "Ignore".
 * - Intercepts send (Enter key + send-button capture) and calls
 *   /api/ext/redact first. Fail-safe: any failure blocks the send.
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
  function decide(res) {
    if (LOGIC && LOGIC.failSafeDecision) return LOGIC.failSafeDecision(res);
    /* Tiny duplicate of failSafeDecision in logic.js (see src/logic.js). */
    if (!res || !res.ok) return { action: 'BLOCK', reason: FAILSAFE_MESSAGE };
    var d = res.decision || {};
    if (d.action === 'REDACT') return { action: 'REDACT', safe_text: d.safe_text || '', count: d.count || 0 };
    if (d.action === 'HOLD' || d.action === 'WARN') return { action: 'HOLD', reason: d.reason || '' };
    if (d.action === 'ALLOW') return { action: 'ALLOW' };
    return { action: 'BLOCK', reason: d.reason || FAILSAFE_MESSAGE };
  }

  var DEBOUNCE_MS = 400;
  var state = {
    adapter: null,
    editor: null,
    spans: [],        // raw spans from last scan: {start,end,label,placeholder}
    ignored: [],      // ignored ranges {start,end}
    redactedArmed: false, // set after REDACT replace; next send goes through
    sending: false,
    siteOn: true
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

  /* ---------- overlay (closed shadow root) ---------- */
  var overlayHost = null, overlayShadow = null, overlayInner = null;
  var hoverCard = null, bannerBar = null, attachBadge = null;

  function ensureOverlay() {
    if (overlayHost) return;
    overlayHost = document.createElement('div');
    overlayHost.setAttribute('data-sf', 'overlay');
    overlayHost.style.cssText = 'position:fixed;z-index:2147483646;pointer-events:none;display:none;overflow:hidden;';
    document.documentElement.appendChild(overlayHost);
    overlayShadow = overlayHost.attachShadow({ mode: 'closed' });
    var style = document.createElement('style');
    style.textContent =
      '.sf-mirror{white-space:pre-wrap;word-wrap:break-word;color:transparent;pointer-events:none;}' +
      '.sf-mirror u{text-decoration:underline wavy #e5484d;text-decoration-thickness:2px;text-underline-offset:3px;background:rgba(229,72,77,.12);pointer-events:auto;cursor:help;}';
    overlayShadow.appendChild(style);
    overlayInner = document.createElement('div');
    overlayInner.className = 'sf-mirror';
    overlayShadow.appendChild(overlayInner);
  }

  function positionOverlay() {
    if (!state.editor || !overlayHost) return;
    var r = state.editor.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) { overlayHost.style.display = 'none'; return; }
    var cs = getComputedStyle(state.editor);
    overlayHost.style.display = 'block';
    overlayHost.style.left = (r.left + window.scrollX) + 'px';
    overlayHost.style.top = (r.top + window.scrollY) + 'px';
    overlayHost.style.width = r.width + 'px';
    overlayHost.style.height = r.height + 'px';
    overlayInner.style.font = cs.font;
    overlayInner.style.padding = cs.padding;
    overlayInner.style.lineHeight = cs.lineHeight;
    overlayInner.style.letterSpacing = cs.letterSpacing;
  }

  function renderOverlay() {
    ensureOverlay();
    if (!state.editor || !state.spans.length) {
      if (overlayHost) overlayHost.style.display = 'none';
      return;
    }
    var text = editorText(state.editor);
    var ranges = toRanges(state.spans.map(function (s, i) {
      return { start: s.start, end: s.end, idx: i };
    }), text.length).map(function (r) { return r; });
    // Pair ranges back to span metadata by overlap.
    positionOverlay();
    var html = '', pos = 0, k;
    var esc = function (t) {
      return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    };
    var sorted = ranges.slice().sort(function (a, b) { return a.start - b.start; });
    for (k = 0; k < sorted.length; k++) {
      var r = sorted[k];
      if (r.start < pos) continue;
      // Skip ranges the user ignored.
      var isIgnored = state.ignored.some(function (g) { return r.start >= g.start && r.end <= g.end; });
      html += esc(text.slice(pos, r.start));
      if (isIgnored) {
        html += esc(text.slice(r.start, r.end));
      } else {
        var meta = null;
        for (var m = 0; m < state.spans.length; m++) {
          var sp = state.spans[m];
          if (sp.start < r.end && sp.end > r.start) { meta = sp; break; }
        }
        html += '<u data-sf-idx="' + (meta ? state.spans.indexOf(meta) : -1) + '">' +
          esc(text.slice(r.start, r.end)) + '</u>';
      }
      pos = r.end;
    }
    html += esc(text.slice(pos));
    overlayInner.innerHTML = html;
    var underlines = overlayShadow.querySelectorAll('u');
    underlines.forEach(function (u) {
      u.addEventListener('mouseenter', function () {
        var idx = Number(u.getAttribute('data-sf-idx'));
        showHoverCard(u, state.spans[idx]);
      });
      u.addEventListener('mouseleave', function () { scheduleHideCard(); });
    });
  }

  /* ---------- hover card ---------- */
  var hideTimer = null;
  function ensureHoverCard() {
    if (hoverCard) return hoverCard;
    hoverCard = document.createElement('div');
    hoverCard.setAttribute('data-sf', 'hovercard');
    hoverCard.style.cssText = 'position:fixed;z-index:2147483647;display:none;max-width:280px;' +
      'background:#1c1c1f;color:#f5f5f5;border:1px solid #444;border-radius:8px;' +
      'padding:10px 12px;font:13px/1.4 system-ui,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.4);';
    hoverCard.addEventListener('mouseenter', function () {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    });
    hoverCard.addEventListener('mouseleave', function () { hideHoverCard(); });
    document.documentElement.appendChild(hoverCard);
    return hoverCard;
  }
  function showHoverCard(anchor, span) {
    if (!span) return;
    var card = ensureHoverCard();
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    var r = anchor.getBoundingClientRect();
    card.innerHTML = '';
    var title = document.createElement('div');
    title.textContent = 'Found: ' + (span.label || 'sensitive detail');
    title.style.fontWeight = '600';
    title.style.marginBottom = '4px';
    var sub = document.createElement('div');
    sub.textContent = span.placeholder ? ('Placeholder: ' + span.placeholder) : 'This text looks sensitive.';
    sub.style.opacity = '0.85';
    sub.style.marginBottom = '8px';
    var row = document.createElement('div');
    row.style.display = 'flex';
    row.style.gap = '8px';
    var btnReplace = document.createElement('button');
    btnReplace.textContent = 'Replace with placeholder';
    var btnIgnore = document.createElement('button');
    btnIgnore.textContent = 'Ignore';
    [btnReplace, btnIgnore].forEach(function (b) {
      b.style.cssText = 'flex:1;cursor:pointer;border:1px solid #666;background:#2d2d30;color:#fff;border-radius:6px;padding:6px 8px;';
    });
    btnReplace.addEventListener('click', function () {
      replaceSpanWithPlaceholder(span);
      hideHoverCard();
    });
    btnIgnore.addEventListener('click', function () {
      state.ignored.push({ start: span.start, end: span.end });
      hideHoverCard();
      renderOverlay();
    });
    row.appendChild(btnReplace);
    row.appendChild(btnIgnore);
    card.appendChild(title);
    card.appendChild(sub);
    card.appendChild(row);
    card.style.display = 'block';
    card.style.left = Math.min(window.innerWidth - 296, r.left + window.scrollX) + 'px';
    card.style.top = (r.bottom + window.scrollY + 6) + 'px';
  }
  function scheduleHideCard() {
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(hideHoverCard, 350);
  }
  function hideHoverCard() {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    if (hoverCard) hoverCard.style.display = 'none';
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

  /* ---------- banner / notices ---------- */
  function ensureBanner() {
    if (bannerBar) return bannerBar;
    bannerBar = document.createElement('div');
    bannerBar.setAttribute('data-sf', 'banner');
    bannerBar.style.cssText = 'position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:2147483647;' +
      'display:none;max-width:min(560px,90vw);background:#1c1c1f;color:#fff;border:1px solid #555;' +
      'border-radius:10px;padding:10px 14px;font:13px/1.45 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.5);';
    document.documentElement.appendChild(bannerBar);
    return bannerBar;
  }
  function showBanner(html, buttons) {
    var bar = ensureBanner();
    bar.innerHTML = '';
    var msg = document.createElement('span');
    msg.innerHTML = html;
    bar.appendChild(msg);
    (buttons || []).forEach(function (def) {
      var b = document.createElement('button');
      b.textContent = def.label;
      b.style.cssText = 'margin-left:8px;cursor:pointer;border:1px solid #777;background:#2d2d30;color:#fff;border-radius:6px;padding:5px 10px;';
      b.addEventListener('click', function () { hideBanner(); if (def.onClick) def.onClick(); });
      bar.appendChild(b);
    });
    var x = document.createElement('button');
    x.textContent = '\u00d7';
    x.setAttribute('aria-label', 'Dismiss');
    x.style.cssText = 'margin-left:8px;cursor:pointer;border:none;background:transparent;color:#aaa;font-size:15px;';
    x.addEventListener('click', hideBanner);
    bar.appendChild(x);
    bar.style.display = 'block';
    clearTimeout(showBanner._t);
    showBanner._t = setTimeout(hideBanner, 12000);
  }
  function hideBanner() { if (bannerBar) bannerBar.style.display = 'none'; }
  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  /* ---------- attachment badge (warning only) ---------- */
  function ensureAttachBadge() {
    if (attachBadge) return attachBadge;
    attachBadge = document.createElement('div');
    attachBadge.setAttribute('data-sf', 'attachbadge');
    attachBadge.style.cssText = 'position:fixed;z-index:2147483646;display:none;background:#3a2b00;color:#ffd666;' +
      'border:1px solid #8a6d00;border-radius:8px;padding:6px 10px;font:12px/1.4 system-ui,sans-serif;';
    attachBadge.textContent = '\u26a0 Attachment present \u2014 files are not scanned, only this warning is shown.';
    document.documentElement.appendChild(attachBadge);
    return attachBadge;
  }
  function updateAttachBadge() {
    if (!state.adapter || !state.editor) return;
    var badge = ensureAttachBadge();
    var found = null;
    for (var i = 0; i < state.adapter.attachment.length; i++) {
      try {
        var inputs = document.querySelectorAll(state.adapter.attachment[i]);
        for (var k = 0; k < inputs.length; k++) {
          var el = inputs[k];
          if (el.tagName === 'INPUT' && el.type === 'file') {
            if (el.files && el.files.length) { found = el; break; }
          } else { found = el; break; }
        }
      } catch (e) { /* ignore */ }
      if (found) break;
    }
    if (found) {
      var r = state.editor.getBoundingClientRect();
      badge.style.display = 'block';
      badge.style.left = (r.left + window.scrollX) + 'px';
      badge.style.top = Math.max(0, (r.top + window.scrollY - 34)) + 'px';
    } else {
      badge.style.display = 'none';
    }
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
  function handleRedactResult(res) {
    var d = decide(res);
    if (d.action === 'ALLOW') {
      state.redactedArmed = true; // scanned clean / app allowed: let this send through
      doSend();
      return;
    }
    if (d.action === 'REDACT') {
      setEditorText(state.editor, d.safe_text);
      state.spans = [];
      renderOverlay();
      state.redactedArmed = true; // user must press send again to send the safe text
      var n = d.count || 0;
      showBanner(escHtml(n + ' detail' + (n === 1 ? '' : 's') + ' protected \u2014 press send again to send the protected text.'));
      return;
    }
    if (d.action === 'HOLD') {
      showBanner(escHtml(d.reason || 'Please review before sending.'), [
        { label: 'Send anyway', onClick: function () { state.redactedArmed = true; doSend(); } },
        { label: 'Cancel', onClick: function () { /* stay; user edits */ } }
      ]);
      return;
    }
    // BLOCK (including fail-safe when the app is unreachable)
    var reason = d.reason || FAILSAFE_MESSAGE;
    showBanner('<strong>Blocked \u2014 not sent.</strong> ' + escHtml(reason));
  }

  function requestSendCheck() {
    if (state.sending) return;
    if (state.redactedArmed) { state.redactedArmed = false; return; } // user confirmed; let it go
    if (!state.siteOn) return; // extension off for this site: do not interfere
    state.sending = true;
    var text = editorText(state.editor);
    sendToBg({ type: 'SF_REDACT', text: text, host: location.hostname }).then(function (res) {
      state.sending = false;
      handleRedactResult(res);
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
      if (!state.siteOn || state.redactedArmed || state.sending) return;
      ev.preventDefault();
      ev.stopPropagation();
      requestSendCheck();
    }, true);
    document.addEventListener('click', function (ev) {
      if (!state.adapter || !state.siteOn || state.redactedArmed || state.sending) return;
      var t = ev.target;
      if (!(t instanceof Element)) return;
      var btn = qs(state.adapter.sendButton, document);
      if (btn && (t === btn || btn.contains(t))) {
        ev.preventDefault();
        ev.stopPropagation();
        requestSendCheck();
      }
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
      if (node.nodeValue && node.nodeValue.indexOf('\u3010') !== -1) out.push(node);
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
    if (texts.indexOf('\u3010') === -1) return; // no placeholders on screen
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
            frag.style.cssText = 'background:rgba(90,140,255,.15);border-bottom:1px dotted #5a8cff;';
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
    editor.addEventListener('input', scheduleScan);
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
    if (state.editor && !document.contains(state.editor)) {
      state.editor = null;
      if (overlayHost) overlayHost.style.display = 'none';
    }
    if (state.editor) { positionOverlay(); updateAttachBadge(); }
  }

  bindSendInterception();
  setInterval(tick, 1500);
  tick();
  window.addEventListener('resize', positionOverlay);
  window.addEventListener('scroll', positionOverlay, true);
})();
