'use strict';

/* T63b regressions: the send-time card must always show for BLOCK/HOLD/
 * REDACT, even when PII underline/hover state is present, and the
 * capture-phase guard and the adapter path must never double-handle one
 * send (one send = one check = one card).
 *
 * There is no DOM in Node, so this file drives the REAL
 * extension/src/content.js (plus logic.js/adapters.js) with a small
 * faithful stub: capture/bubble dispatch where stopPropagation does NOT
 * stop same-node listeners (DOM semantics), programmable timers via the
 * real clock, and a programmable background responder with controllable
 * latency. Run with: node --test extension/tests/
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = path.join(__dirname, '..', 'src');

/* --- tiny CSS matcher: only the shapes the fixtures use --- */

function parseAttrMini(inner) {
  const m = inner.match(/^\s*([a-zA-Z0-9_-]+)\s*(?:(=\*=?|\*=|\^=|\$=|=)\s*"([^"]*)"\s*(i)?)?\s*$/);
  if (!m) throw new Error('unsupported attr: [' + inner + ']');
  return { name: m[1].toLowerCase(), op: m[2] || null, value: m[3] || '', ci: !!m[4] };
}

function matchCompoundMini(sel, el) {
  let s = sel;
  const nots = [];
  s = s.replace(/:not\(([^()]*)\)/g, (_, inner) => { nots.push(inner.trim()); return ''; });
  const tagM = s.match(/^([a-zA-Z][a-zA-Z0-9-]*|\*)/);
  if (tagM && tagM[1] !== '*' && el.tagName.toLowerCase() !== tagM[1].toLowerCase()) return false;
  const idM = s.match(/#([a-zA-Z0-9_-]+)/);
  if (idM && el.id !== idM[1]) return false;
  const attrRe = /\[([^\]]*)\]/g;
  let m;
  while ((m = attrRe.exec(s))) {
    const a = parseAttrMini(m[1]);
    const v = el.getAttribute(a.name);
    if (v === null || v === undefined) return false;
    if (a.op === null) continue;
    const hay = a.ci ? String(v).toLowerCase() : String(v);
    const ndl = a.ci ? a.value.toLowerCase() : a.value;
    if ((a.op === '=' || a.op === '==') && hay !== ndl) return false;
    if (a.op === '*=' && !hay.includes(ndl)) return false;
    if (a.op === '^=' && !hay.startsWith(ndl)) return false;
    if (a.op === '$=' && !hay.endsWith(ndl)) return false;
  }
  for (const n of nots) {
    if (matchCompoundMini(n, el)) return false;
  }
  return true;
}

/* Split a selector on descendant combinators without breaking quoted
 * attribute values such as [aria-label="Send message"]. */
function splitSelectorMini(sel) {
  const parts = [];
  let cur = '', depth = 0, quote = null;
  for (const ch of sel.trim()) {
    if (quote) {
      cur += ch;
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") { quote = ch; cur += ch; }
    else if (ch === '[') { depth++; cur += ch; }
    else if (ch === ']') { depth--; cur += ch; }
    else if (/\s/.test(ch) && depth === 0) {
      if (cur) { parts.push(cur); cur = ''; }
    } else cur += ch;
  }
  if (cur) parts.push(cur);
  return parts;
}

/* Split a selector list on top-level commas (real querySelector/closest
 * accept lists and match when ANY branch matches). */
function splitListMini(sel) {
  const out = [];
  let cur = '', depth = 0, quote = null;
  for (const ch of sel) {
    if (quote) {
      cur += ch;
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") { quote = ch; cur += ch; }
    else if (ch === '[' || ch === '(') { depth++; cur += ch; }
    else if (ch === ']' || ch === ')') { depth--; cur += ch; }
    else if (ch === ',' && depth === 0) {
      if (cur.trim()) out.push(cur.trim());
      cur = '';
    } else cur += ch;
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}

function matchesSelectorMini(sel, el) {
  return splitListMini(sel).some((branch) => {
    const parts = splitSelectorMini(branch);
    let node = el;
    for (let p = parts.length - 1; p >= 0; p--) {
      while (node && !matchCompoundMini(parts[p], node)) node = node.parent;
      if (!node) return false;
      node = node.parent;
    }
    return true;
  });
}

/* --- stub DOM --- */

class MiniEvent {
  constructor(type, init) {
    this.type = type;
    this.key = (init && init.key) || '';
    this.shiftKey = !!(init && init.shiftKey);
    this.isComposing = !!(init && init.isComposing);
    this.bubbles = !!(init && init.bubbles);
    this.cancelable = !!(init && init.cancelable);
    this.target = null;
    this.defaultPrevented = false;
    this._stopped = false;
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this._stopped = true; }
}

class MiniElement {
  constructor(tag, doc) {
    this.tagName = String(tag || 'div').toUpperCase();
    this._doc = doc;
    this.id = '';
    this.attrs = {};
    this.children = [];
    this.parent = null;
    this.style = {};
    this.listeners = {};
    this.value = '';
    this.textContent = '';
    this._rect = { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
    this.isContentEditable = false;
    this.type = '';
    this.files = null;
    this.hidden = false;
    this.onmouseenter = null;
    this.onmouseleave = null;
    this.className = '';
  }
  attachShadow() {
    const sh = new MiniShadow();
    if (this._doc) this._doc._shadows.push(sh);
    return sh;
  }
  setAttribute(n, v) {
    n = String(n).toLowerCase();
    this.attrs[n] = String(v);
    if (n === 'id') this.id = String(v);
    if (n === 'type') this.type = String(v);
  }
  getAttribute(n) {
    n = String(n).toLowerCase();
    if (n === 'id') return this.id || null;
    if (n === 'type') return this.type || null;
    return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null;
  }
  addEventListener(type, fn, opts) {
    const capture = opts === true || !!(opts && opts.capture);
    (this.listeners[type] = this.listeners[type] || []).push({ fn, capture });
  }
  appendChild(el) { el.parent = this; this.children.push(el); return el; }
  contains(el) {
    let n = el;
    while (n) { if (n === this) return true; n = n.parent; }
    return false;
  }
  closest(sel) {
    let n = this;
    while (n) {
      try { if (matchesSelectorMini(sel, n)) return n; } catch (e) { return null; }
      n = n.parent;
    }
    return null;
  }
  querySelector(sel) {
    const all = this.querySelectorAll(sel);
    return all.length ? all[0] : null;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (el) => {
      for (const c of el.children) {
        try { if (matchesSelectorMini(sel, c)) out.push(c); } catch (e) { /* bad sel */ }
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  getBoundingClientRect() { return Object.assign({}, this._rect); }
  setRect(r) { this._rect = Object.assign({}, this._rect, r); }
  focus() { if (this._doc) this._doc._focus(this); }
  click() { if (this._doc) this._doc._dispatch(new this._doc._Event('click', { bubbles: true }), this); }
  dispatchEvent(ev) {
    if (this._doc) this._doc._dispatch(ev, this);
    return !ev.defaultPrevented;
  }
  set innerHTML(html) {
    this.children = [];
    // Only shape content.js writes into the mirror: text + <u> spans.
    const re = /<u\b([^>]*)>([\s\S]*?)<\/u>/g;
    let m;
    const unesc = (s) => String(s).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
    while ((m = re.exec(String(html)))) {
      const u = new MiniElement('u', this._doc);
      const attrRe = /([a-zA-Z0-9-]+)="([^"]*)"/g;
      let a;
      while ((a = attrRe.exec(m[1]))) u.setAttribute(a[1], a[2]);
      u.textContent = unesc(m[2]);
      this.appendChild(u);
    }
  }
  get innerText() { return this.textContent; }
}

class MiniShadow {
  constructor() { this.children = []; }
  appendChild(el) { this.children.push(el); return el; }
  byClass(cls) {
    const out = [];
    const walk = (els) => {
      for (const el of els) {
        if (el.className === cls) out.push(el);
        if (el.children) walk(el.children);
      }
    };
    walk(this.children);
    return out;
  }
}

function makeClassList(el) { return ''; }

class MiniDocument {
  constructor(hostname) {
    this._Event = MiniEvent;
    this._docListeners = [];
    this.activeElement = null;
    this.documentElement = new MiniElement('html', this);
    this.body = new MiniElement('body', this);
    this.documentElement.appendChild(this.body);
    this._hostname = hostname;
    this._shadows = [];
  }
  createElement(tag) { return new MiniElement(tag, this); }
  addEventListener(type, fn, opts) {
    const capture = opts === true || !!(opts && opts.capture);
    this._docListeners.push({ type, fn, capture });
  }
  _roots() { return [this.documentElement]; }
  querySelector(sel) {
    const all = this.querySelectorAll(sel);
    return all.length ? all[0] : null;
  }
  querySelectorAll(sel) {
    const out = [];
    for (const root of this._roots()) {
      try { if (matchesSelectorMini(sel, root)) out.push(root); } catch (e) { /* bad sel */ }
      for (const el of root.querySelectorAll(sel)) out.push(el);
    }
    return out;
  }
  contains(el) {
    for (const root of this._roots()) {
      if (root === el || root.contains(el)) return true;
    }
    return false;
  }
  execCommand() { return true; }
  createTreeWalker() { return { nextNode() { return null; } }; }
  _focus(el) {
    const old = this.activeElement;
    this.activeElement = el;
    if (old && old !== el) this._dispatch(new MiniEvent('blur', {}), old);
  }
  /* DOM dispatch: document-capture listeners all run in registration
   * order even after stopPropagation (same node). A capture-phase stop
   * keeps the event from ever reaching the target (how sends are
   * stopped); a target-phase stop only skips the bubble phase. */
  _dispatch(ev, target) {
    ev.target = target;
    for (const l of this._docListeners) {
      if (l.type === ev.type && l.capture) l.fn(ev);
    }
    if (!ev._stopped) {
      const tlist = (target.listeners && target.listeners[ev.type]) || [];
      for (const l of tlist.filter((l) => l.capture)) l.fn(ev);
      for (const l of tlist.filter((l) => !l.capture)) l.fn(ev);
    }
    if (ev.bubbles && !ev._stopped) {
      for (const l of this._docListeners) {
        if (l.type === ev.type && !l.capture) l.fn(ev);
      }
    }
    return !ev.defaultPrevented;
  }
}

/* --- load the real content script in a stub browser --- */

const CONTENT_SRC = fs.readFileSync(path.join(SRC, 'content.js'), 'utf8');
const LOGIC_SRC = fs.readFileSync(path.join(SRC, 'logic.js'), 'utf8');
const ADAPTERS_SRC = fs.readFileSync(path.join(SRC, 'adapters.js'), 'utf8');

function unrefTimer(fn) {
  return (...args) => {
    const t = fn(...args);
    if (t && typeof t.unref === 'function') t.unref();
    return t;
  };
}

class MiniMutationObserver {
  constructor(fn) { this._fn = fn; }
  observe() { /* stub: replies never mutate in these tests */ }
  disconnect() { /* stub */ }
}

function loadExtension(hostname, routes, setup) {
  const doc = new MiniDocument(hostname);
  const fixture = setup ? setup(doc) : {};
  const sent = [];
  const chrome = {
    runtime: {
      lastError: null,
      sendMessage(msg, cb) {
        sent.push({ type: msg.type, text: msg.text || '', at: Date.now() });
        const r = routes[msg.type];
        if (r && r.hang) return; // response never arrives: dropped port / hung app
        const delay = r && typeof r.delay === 'number' ? r.delay : 0;
        const make = r && r.make ? r.make : (() => ({ ok: false, error: 'no route' }));
        // Unref'd: a runaway check loop must not hold the runner open.
        const t = setTimeout(() => cb(make(msg)), delay);
        if (t && typeof t.unref === 'function') t.unref();
      },
    },
  };
  const win = { innerWidth: 1280, innerHeight: 760, addEventListener() { /* stub */ } };
  const ctx = {
    console,
    document: doc,
    window: win,
    location: { hostname },
    chrome,
    getComputedStyle: () => ({}),
    MutationObserver: MiniMutationObserver,
    NodeFilter: { SHOW_TEXT: 4 },
    Element: MiniElement,
    Event: MiniEvent,
    KeyboardEvent: class extends MiniEvent {
      constructor(type, init) { super(type, init); this.code = (init && init.code) || ''; }
    },
    setTimeout: unrefTimer(setTimeout),
    clearTimeout,
    setInterval: unrefTimer(setInterval),
    clearInterval,
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(LOGIC_SRC, ctx, { filename: 'logic.js' });
  vm.runInContext(ADAPTERS_SRC, ctx, { filename: 'adapters.js' });
  vm.runInContext(CONTENT_SRC, ctx, { filename: 'content.js' });
  return { doc, sent, chrome, ctx, fixture };
}

function shadowCard(doc) {
  for (const sh of doc._shadows) {
    const cards = sh.byClass('sf-card');
    if (cards.length) return cards[0];
  }
  return null;
}

function cardState(doc) {
  const card = shadowCard(doc);
  if (!card) return { found: false };
  const bar = card.children[0];
  const title = bar && bar.children[0];
  const body = card.children[1];
  return {
    found: true,
    display: card.style.display || '',
    title: title ? title.textContent : '',
    body: body ? body.textContent : '',
  };
}

function mirrorUnderlines(doc) {
  for (const sh of doc._shadows) {
    const mirrors = sh.byClass('sf-mirror');
    if (mirrors.length) return mirrors[0].children.filter((c) => c.tagName === 'U');
  }
  return [];
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* attachShadow lives on MiniElement so elements created before any
 * patching (the overlay host) get a recorded closed shadow root. */

/* --- fixtures --- */

// Key fragment joined at runtime so no live-key shape is stored here.
const KEY = ['AK', 'IA'].join('') + 'IOSFODNN7EXAMPLE';
const MSG =
  'Draft a reply to Ada Byron, ada.byron@northwind-mail.co, ' +
  'phone +44 20 7946 0958. Also deploy with ' + KEY;

function spansFor(msg) {
  const span = (text, label, placeholder) => {
    const start = msg.indexOf(text);
    assert.ok(start !== -1, 'fixture message must contain ' + text);
    return { start, end: start + text.length, label, placeholder };
  };
  return [
    span('ada.byron@northwind-mail.co', 'email', '[email_1]'),
    span('+44 20 7946 0958', 'phone', '[phone_1]'),
    span(KEY, 'api key', '[api_key_1]'),
  ];
}

const BLOCK_REDACT = () => ({
  ok: true,
  data: { decision: 'BLOCK', safe_text: '', notes: 'An AWS API key \u2014 not sent' },
});

function baseRoutes(spans, redactMake, redactDelay) {
  return {
    SF_GET_STATE: { make: () => ({ ok: true, data: { disabled: false, sites: {} } }) },
    SF_PROT: { make: () => ({ ok: true }) },
    SF_SCAN: { delay: 20, make: () => ({ ok: true, data: { spans, count: spans.length } }) },
    SF_REDACT: { delay: redactDelay, make: redactMake },
    SF_REHYDRATE: { make: () => ({ ok: true, data: { replacements: [] } }) },
  };
}

const redactCount = (sent) => sent.filter((m) => m.type === 'SF_REDACT').length;

function buildTestpage(doc) {
  const form = doc.createElement('form');
  doc.body.appendChild(form);
  const editor = doc.createElement('textarea');
  editor.setAttribute('id', 'composer-text');
  editor.setAttribute('data-sf-test-composer', '');
  editor.setRect({ left: 100, top: 100, right: 740, bottom: 200, width: 640, height: 100 });
  form.appendChild(editor);
  const btn = doc.createElement('button');
  btn.setAttribute('id', 'sendBtn');
  btn.setAttribute('type', 'button');
  btn.textContent = 'Send';
  btn.setRect({ left: 100, top: 220, right: 170, bottom: 250, width: 70, height: 30 });
  form.appendChild(btn);
  const pageSent = [];
  btn.addEventListener('click', () => { pageSent.push(editor.value); });
  const reply = doc.createElement('div');
  reply.setAttribute('id', 'replyArea');
  doc.body.appendChild(reply);
  return { form, editor, btn, pageSent };
}

function buildChatgptLike(doc) {
  const main = doc.createElement('main');
  doc.body.appendChild(main);
  const form = doc.createElement('form');
  main.appendChild(form);
  const editor = doc.createElement('textarea');
  editor.setAttribute('id', 'mobile-composer-prompt');
  editor.setAttribute('aria-label', 'Chat with ChatGPT');
  editor.setAttribute('placeholder', 'Ask ChatGPT');
  editor.setRect({ left: 100, top: 100, right: 740, bottom: 200, width: 640, height: 100 });
  form.appendChild(editor);
  const btn = doc.createElement('button');
  btn.setAttribute('type', 'submit');
  btn.setAttribute('aria-label', 'Send message');
  btn.textContent = 'Send';
  btn.setRect({ left: 660, top: 220, right: 740, bottom: 250, width: 80, height: 30 });
  form.appendChild(btn);
  const pageSent = [];
  btn.addEventListener('click', () => { pageSent.push(editor.value); });
  return { form, editor, btn, pageSent };
}

test('T63b PII spans present + key: BLOCK send-time card shows and stays', async () => {
  // Slow redact (500ms): the hover-hide timer (300ms) fires BEFORE the
  // BLOCK card shows. The send card must still appear and stay.
  const spans = spansFor(MSG);
  const { doc, sent, fixture } = loadExtension(
    '127.0.0.1', baseRoutes(spans, BLOCK_REDACT, 500), buildTestpage);
  const { editor, btn, pageSent } = fixture;
  editor.value = MSG;
  editor.focus();
  await sleep(650); // debounce 400ms + scan round trip
  const underlines = mirrorUnderlines(doc);
  assert.equal(underlines.length, 3, 'PII + key underlines must render, got ' + underlines.length);
  // Mouse crosses an underline on its way to Send: hover card opens,
  // then the mouse leaves and the hover-hide timer is armed.
  doc._dispatch(new MiniEvent('mouseenter', {}), underlines[0]);
  let hover = cardState(doc);
  assert.equal(hover.display, 'block');
  assert.match(hover.title, /Found: email/);
  doc._dispatch(new MiniEvent('mouseleave', {}), underlines[0]);
  // Click Send.
  const before = redactCount(sent);
  const stopped = doc._dispatch(new MiniEvent('click', { bubbles: true }), btn);
  assert.equal(stopped, false, 'intercepted sends never reach the page handler');
  assert.deepEqual(pageSent, [], 'BLOCK verdict must not send');
  await sleep(900); // hover-hide (300ms) + BLOCK card (500ms) both fired
  assert.equal(redactCount(sent) - before, 1, 'one send = one check');
  let card = cardState(doc);
  assert.equal(card.display, 'block', 'BLOCK card must be visible, got ' + JSON.stringify(card));
  assert.match(card.title, /Blocked/);
  assert.match(card.body, /not sent/);
  await sleep(800); // no stray timer may close it afterwards
  card = cardState(doc);
  assert.equal(card.display, 'block', 'BLOCK card must stay, got ' + JSON.stringify(card));
  assert.match(card.title, /Blocked/);
  // An overlay re-render (a scan answering after the card) must not
  // close the send card either.
  editor.dispatchEvent(new MiniEvent('input', { bubbles: true }));
  await sleep(700);
  card = cardState(doc);
  assert.equal(card.display, 'block', 're-render must keep the card, got ' + JSON.stringify(card));
  assert.match(card.title, /Blocked/);
});

test('T63b guard and adapter never double-handle one send', async () => {
  // chatgpt-like button: the adapter sendButton selector AND the
  // capture-guard send-button heuristic match the same button, so both
  // document-capture listeners run for one click (stopPropagation does
  // not stop same-node listeners).
  const spans = spansFor(MSG);
  const { doc, sent, fixture } = loadExtension(
    'chatgpt.com', baseRoutes(spans, BLOCK_REDACT, 30), buildChatgptLike);
  const { editor, btn, pageSent } = fixture;
  editor.value = MSG;
  editor.focus();
  await sleep(650);
  assert.ok(mirrorUnderlines(doc).length > 0, 'underlines must render');
  // Click path: exactly one check, page never sent, card visible.
  let before = redactCount(sent);
  doc._dispatch(new MiniEvent('click', { bubbles: true }), btn);
  await sleep(400);
  assert.equal(redactCount(sent) - before, 1, 'one click = one check, got ' +
    (redactCount(sent) - before));
  assert.deepEqual(pageSent, [], 'BLOCK verdict must not send');
  let card = cardState(doc);
  assert.equal(card.display, 'block', 'BLOCK card must be visible, got ' + JSON.stringify(card));
  assert.match(card.title, /Blocked/);
  // Enter path: exactly one check as well.
  before = redactCount(sent);
  const enter = new MiniEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  doc._dispatch(enter, editor);
  assert.equal(enter.defaultPrevented, true, 'adapter must intercept Enter');
  await sleep(400);
  assert.equal(redactCount(sent) - before, 1, 'one Enter = one check, got ' +
    (redactCount(sent) - before));
  card = cardState(doc);
  assert.equal(card.display, 'block', 'BLOCK card must be visible, got ' + JSON.stringify(card));
});

test('T63b hung send check still shows a fail-safe card, never silently', async () => {
  // The live bug: the send is stopped but no card ever appears. One way
  // that happens is the redact answer never resolving (dropped port);
  // the send must then fail closed AND visibly instead of hanging mute.
  const spans = spansFor(MSG);
  const routes = baseRoutes(spans, BLOCK_REDACT, 30);
  routes.SF_REDACT = { hang: true };
  const { doc, sent, fixture } = loadExtension('127.0.0.1', routes, buildTestpage);
  const { editor, btn, pageSent } = fixture;
  editor.value = MSG;
  editor.focus();
  await sleep(650);
  assert.ok(mirrorUnderlines(doc).length > 0, 'underlines must render');
  doc._dispatch(new MiniEvent('click', { bubbles: true }), btn);
  await sleep(400);
  assert.deepEqual(pageSent, [], 'hung check must not send');
  // T63c: the send waits up to 45 s with a Checking card instead of the
  // old 1.5 s fail-safe. The full 45 s wait plus the retry card is
  // covered by the T63c timeout test; here the check stays visibly
  // pending and never sends while it hangs.
  let card = cardState(doc);
  assert.equal(card.display, 'block', 'Checking card must show, got ' + JSON.stringify(card));
  assert.match(card.title, /Checking/);
  await sleep(2000); // past the old timeout: still checking, never failed
  card = cardState(doc);
  assert.equal(card.display, 'block', 'still Checking, got ' + JSON.stringify(card));
  assert.match(card.title, /Checking/);
  assert.deepEqual(pageSent, [], 'hung check must not send');
});

test('T63b ALLOW resend is delivered exactly once with one check', async () => {
  // Same-button double handling: the adapter consumes the armed pass and
  // the guard must let that same event through, not start another check.
  const routes = baseRoutes([], () => ({ ok: true, data: { decision: 'ALLOW', safe_text: MSG, notes: '' } }), 30);
  const { doc, sent, fixture } = loadExtension('chatgpt.com', routes, buildChatgptLike);
  const { editor, btn, pageSent } = fixture;
  editor.value = MSG;
  editor.focus();
  await sleep(650);
  doc._dispatch(new MiniEvent('click', { bubbles: true }), btn);
  await sleep(700);
  assert.equal(redactCount(sent), 1, 'one click = one check, got ' + redactCount(sent));
  assert.deepEqual(pageSent, [MSG], 'ALLOW must deliver the send exactly once');
});


