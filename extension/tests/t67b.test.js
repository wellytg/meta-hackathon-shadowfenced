'use strict';

/* T67b: the file guard must snapshot the file list BEFORE clearing the
 * input (change) or cancelling the event (drop/paste). `input.files` is
 * a LIVE FileList in a real browser: the old change handler aliased it,
 * then `input.value = ''` emptied that same list, then fgCheckFiles
 * copied zero files and returned silently — no card, no scan, and clean
 * files never passed through.
 *
 * These tests drive the REAL extension/src/content.js (plus
 * logic.js/adapters.js) in a stub browser, like the T63b tests: window
 * capture listeners run first (stopImmediatePropagation keeps the event
 * from the page, as in the browser), then the document/target phases.
 * Run with: node --test extension/tests/
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = path.join(__dirname, '..', 'src');

/* --- tiny CSS matcher: only the shapes the fixtures use (as in T63b) --- */

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
    this.dataTransfer = (init && init.dataTransfer) || null;
    this.clipboardData = (init && init.clipboardData) || null;
    this.target = null;
    this.defaultPrevented = false;
    this._stopped = false;
    this._immStopped = false;
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this._stopped = true; }
  stopImmediatePropagation() { this._immStopped = true; this._stopped = true; }
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

class MiniMutationObserver {
  constructor(fn) { this._fn = fn; }
  observe() { /* stub */ }
  disconnect() { /* stub */ }
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

function loadExtension(hostname, routes) {
  const doc = new MiniDocument(hostname);
  const sent = [];
  const chrome = {
    runtime: {
      lastError: null,
      sendMessage(msg, cb) {
        sent.push(Object.assign({ at: Date.now() }, msg));
        const r = routes[msg.type];
        if (r && r.hang) return;
        const delay = r && typeof r.delay === 'number' ? r.delay : 0;
        const make = r && r.make ? r.make : (() => ({ ok: false, error: 'no route' }));
        const t = setTimeout(() => cb(make(msg)), delay);
        if (t && typeof t.unref === 'function') t.unref();
      },
    },
  };
  const winListeners = {};
  const win = {
    innerWidth: 1280,
    innerHeight: 760,
    _listeners: winListeners,
    addEventListener(type, fn, opts) {
      const capture = opts === true || !!(opts && opts.capture);
      (winListeners[type] = winListeners[type] || []).push({ fn, capture });
    },
  };
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
    btoa: (s) => Buffer.from(String(s), 'binary').toString('base64'),
    atob: (s) => Buffer.from(String(s), 'base64').toString('binary'),
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
  return { doc, win, sent, chrome, ctx };
}

/* Window capture listeners run first; a capture-phase stop keeps the
 * event from ever reaching the target (how the file guard stops the
 * site seeing the file). */
function dispatchWindowEvent(win, doc, ev, target) {
  ev.target = target;
  const list = ((win._listeners || {})[ev.type] || []).slice();
  for (const l of list) {
    l.fn(ev);
    if (ev._immStopped) break;
  }
  if (!ev._stopped && !ev._immStopped) doc._dispatch(ev, target);
  return !ev.defaultPrevented;
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
  const btns = card.children[2];
  return {
    found: true,
    display: card.style.display || '',
    tone: bar ? bar.attrs['data-tone'] : '',
    title: title ? title.textContent : '',
    body: body ? body.textContent : '',
    buttons: btns ? btns.children.map((b) => b.textContent) : [],
  };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* --- file fixtures --- */

function makeFile(name, bytes) {
  return {
    name,
    size: bytes.length,
    type: 'text/plain',
    lastModified: 1720000000000,
    arrayBuffer: () => Promise.resolve(Uint8Array.from(bytes).buffer),
  };
}

/* A file input whose .files is a LIVE list: clearing .value drains the
 * same object, exactly as in a real browser. */
function buildLiveFileInput(doc, files) {
  const input = doc.createElement('input');
  input.setAttribute('type', 'file');
  input.setAttribute('id', 'fileInput');
  doc.body.appendChild(input);
  const live = {};
  files.forEach((f, i) => { live[i] = f; });
  live.length = files.length;
  input.files = live;
  let v = 'C:\\fakepath\\' + (files[0] ? files[0].name : '');
  Object.defineProperty(input, 'value', {
    configurable: true,
    get() { return v; },
    set(nv) {
      v = String(nv);
      if (v === '') {
        for (const k of Object.keys(live)) delete live[k];
        live.length = 0;
      }
    },
  });
  return { input, live };
}

function buildLiveTransfer(files) {
  const live = {};
  files.forEach((f, i) => { live[i] = f; });
  live.length = files.length;
  const drain = () => {
    for (const k of Object.keys(live)) delete live[k];
    live.length = 0;
  };
  return { files: live, types: ['Files'], _drain: drain };
}

function fileGuardRoutes() {
  return {
    SF_GET_STATE: { make: () => ({ ok: true, data: { disabled: false, sites: {} } }) },
    SF_PROT: { make: () => ({ ok: true }) },
    SF_SCAN: { make: () => ({ ok: true, data: { spans: [], count: 0 } }) },
    SF_REHYDRATE: { make: () => ({ ok: true, data: { replacements: [] } }) },
    SF_SCAN_FILE: {
      delay: 20,
      make: () => ({
        ok: true,
        data: { decision: 'CLEAN', scan_id: 's1', deep_check: { available: false } },
      }),
    },
  };
}

const scanFileCalls = (sent) => sent.filter((m) => m.type === 'SF_SCAN_FILE');

test('T67b change still scans the file after the input is cleared', async () => {
  const { doc, win, sent } = loadExtension('127.0.0.1', fileGuardRoutes());
  const bytes = [104, 105]; // 'hi'
  const file = makeFile('clean_meeting_notes.txt', bytes);
  const { input, live } = buildLiveFileInput(doc, [file]);
  const pageReceived = [];
  input.addEventListener('change', () => {
    pageReceived.push(input.files ? input.files.length : 0);
  });
  dispatchWindowEvent(win, doc, new MiniEvent('change', { bubbles: true }), input);
  assert.equal(input.value, '', 'the guard must clear the input');
  assert.equal(live.length, 0, 'harness check: clearing drained the live list');
  await sleep(300);
  const scans = scanFileCalls(sent);
  assert.equal(scans.length, 1, 'the drained list must still be scanned, got ' + scans.length);
  assert.equal(scans[0].name, 'clean_meeting_notes.txt');
  assert.deepEqual(
    Array.from(Buffer.from(scans[0].dataBase64, 'base64')),
    bytes,
    'the scan must carry the intercepted file bytes');
  assert.deepEqual(pageReceived, [], 'the site must never receive the intercepted file');
  const card = cardState(doc);
  assert.equal(card.display, 'block', 'a result card must show, got ' + JSON.stringify(card));
  assert.match(card.title, /Checked/);
  assert.match(card.title, /clean_meeting_notes\.txt/);
  // The cleared file passes straight through when attached again.
  const again = buildLiveFileInput(doc, [file]);
  const pageReceived2 = [];
  again.input.addEventListener('change', () => {
    pageReceived2.push(again.input.files ? again.input.files.length : 0);
  });
  dispatchWindowEvent(win, doc, new MiniEvent('change', { bubbles: true }), again.input);
  assert.notEqual(again.input.value, '', 'a cleared repeat must not be cleared again');
  assert.deepEqual(pageReceived2, [1], 'a cleared repeat must reach the site untouched');
});

test('T67b drop still scans the file when the transfer drains on cancel', async () => {
  const { doc, win, sent } = loadExtension('127.0.0.1', fileGuardRoutes());
  const zone = doc.createElement('div');
  zone.setAttribute('id', 'dropZone');
  doc.body.appendChild(zone);
  const file = makeFile('clean_meeting_notes.txt', [104, 105]);
  const dt = buildLiveTransfer([file]);
  const pageReceived = [];
  zone.addEventListener('drop', () => { pageReceived.push('drop'); });
  const ev = new MiniEvent('drop', { bubbles: true, cancelable: true });
  ev.dataTransfer = dt;
  const origPrevent = ev.preventDefault.bind(ev);
  ev.preventDefault = () => {
    origPrevent();
    // Models the T67b hazard class for drops: anything that mutates or
    // releases the drag store between interception and read must not
    // lose the files — the guard snapshots dt.files on entry.
    dt._drain();
  };
  dispatchWindowEvent(win, doc, ev, zone);
  assert.equal(dt.files.length, 0, 'harness check: cancelling drained the live transfer');
  await sleep(300);
  const scans = scanFileCalls(sent);
  assert.equal(scans.length, 1, 'the drained transfer must still be scanned, got ' + scans.length);
  assert.equal(scans[0].name, 'clean_meeting_notes.txt');
  assert.deepEqual(pageReceived, [], 'the site must never receive the intercepted drop');
  const card = cardState(doc);
  assert.equal(card.display, 'block', 'a result card must show, got ' + JSON.stringify(card));
  assert.match(card.title, /Checked/);
});

test('T67b an interception that yields zero files still shows Not checked', async () => {
  const { doc, win, sent } = loadExtension('127.0.0.1', fileGuardRoutes());
  const input = doc.createElement('input');
  input.setAttribute('type', 'file');
  input.setAttribute('id', 'fileInput');
  doc.body.appendChild(input);
  // A FileList drained between the interception check and the read
  // (the T67b shape): nonzero length when the guard decides to
  // intercept, empty when it copies the files out.
  let reads = 0;
  const flapping = {};
  Object.defineProperty(flapping, 'length', {
    get() { reads += 1; return reads === 1 ? 1 : 0; },
  });
  input.files = flapping;
  input.value = 'C:\\fakepath\\notes.txt';
  const pageReceived = [];
  input.addEventListener('change', () => { pageReceived.push('change'); });
  dispatchWindowEvent(win, doc, new MiniEvent('change', { bubbles: true }), input);
  await sleep(200);
  assert.deepEqual(pageReceived, [], 'the intercepted event must not reach the site');
  assert.equal(scanFileCalls(sent).length, 0, 'there is nothing to scan');
  const card = cardState(doc);
  assert.equal(card.display, 'block', 'fail visible: a card must show, got ' + JSON.stringify(card));
  assert.match(card.title, /Not checked/);
});
