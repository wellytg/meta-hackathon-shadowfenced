'use strict';

/* T63c honest wait states: a send shows a Checking card at once (amber,
 * elapsed seconds), waits up to 45 s, and maps failures honestly:
 * network error -> "Shadowfenced isn't running", 401/403 -> "Not paired",
 * 45 s silence -> "Still no answer" with Retry. One send = one check =
 * one card. Run with: node --test extension/tests/
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const logic = require('../src/logic.js');

const SRC = path.join(__dirname, '..', 'src');

/* --- stub DOM (same faithful subset as t63b.test.js) --- */

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

/* Minimal selector support: the fixtures only need #id matching
 * (the testpage adapter uses '#composer-text' and '#sendBtn'). */
function matchesIdSelector(sel, el) {
  if (!el || !el.id) return false;
  return String(sel).split(',').some((branch) => branch.indexOf('#' + el.id) !== -1);
}

function walkTree(el, out) {
  out.push(el);
  for (const c of el.children) walkTree(c, out);
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
      if (matchesIdSelector(sel, n)) return n;
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
    const all = [];
    walkTree(this, all);
    for (const el of all) {
      if (el !== this && matchesIdSelector(sel, el)) out.push(el);
    }
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
  set innerHTML(html) { this.children = []; this.textContent = String(html); }
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
  querySelector(sel) {
    const all = this.querySelectorAll(sel);
    return all.length ? all[0] : null;
  }
  querySelectorAll(sel) {
    const out = [];
    const all = [];
    walkTree(this.documentElement, all);
    for (const el of all) {
      if (matchesIdSelector(sel, el)) out.push(el);
    }
    return out;
  }
  contains(el) {
    return this.documentElement === el || this.documentElement.contains(el);
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

function cardButtons(doc) {
  const card = shadowCard(doc);
  if (!card) return [];
  const btns = card.children[2];
  return btns ? btns.children.slice() : [];
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Key fragment joined at runtime so no live-key shape is stored here.
const KEY = ['AK', 'IA'].join('') + 'IOSFODNN7EXAMPLE';
const MSG = 'Draft a reply to Ada Byron. Also deploy with ' + KEY;

const BLOCK_REDACT = () => ({
  ok: true,
  data: { decision: 'BLOCK', safe_text: '', notes: 'An AWS API key \u2014 not sent' },
});

function baseRoutes(redactMake, redactDelay) {
  return {
    SF_GET_STATE: { make: () => ({ ok: true, data: { disabled: false, sites: {} } }) },
    SF_PROT: { make: () => ({ ok: true }) },
    SF_SCAN: { delay: 20, make: () => ({ ok: true, data: { spans: [], count: 0 } }) },
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
  btn.setRect({ left: 100, top: 220, right: 170, bottom: 250, width: 80, height: 30 });
  form.appendChild(btn);
  const pageSent = [];
  btn.addEventListener('click', () => { pageSent.push(editor.value); });
  const reply = doc.createElement('div');
  reply.setAttribute('id', 'replyArea');
  doc.body.appendChild(reply);
  return { form, editor, btn, pageSent };
}

function loadTestpage(hostname, routes) {
  // The fixture exists before the content script evaluates, so its
  // boot tick attaches the adapter and editor at once (like t63b).
  return loadExtension(hostname, routes, buildTestpage);
}

/* --- logic-level card mapping --- */

test('T63c logic: timeout offers retry, 401/403 says not paired', () => {
  const retry = logic.sendFailureCard({ timeout: true });
  assert.equal(retry.send, false);
  assert.match(retry.title, /Still no answer/);
  assert.match(retry.body, /nothing was sent/);
  assert.deepEqual(retry.buttons, ['retry', 'cancel']);

  for (const status of [401, 403]) {
    const card = logic.sendFailureCard({ status });
    assert.equal(card.send, false);
    assert.match(card.title, /Not paired/);
    assert.match(card.body, /paste the pairing token/);
  }

  const down = logic.sendFailureCard({});
  assert.match(down.title, /isn't running/);

  // The capture guard mirrors the same mapping.
  const guardRetry = logic.captureGuardCard({ ok: false, timeout: true, text: 'hi' });
  assert.equal(guardRetry.send, false);
  assert.deepEqual(guardRetry.buttons, ['retry', 'cancel']);
  const guardPaired = logic.captureGuardCard({ ok: false, status: 401, text: 'hi' });
  assert.equal(guardPaired.send, false);
  assert.match(guardPaired.title, /Not paired/);
  const guardDown = logic.captureGuardCard({ ok: false, text: 'hi' });
  assert.deepEqual(guardDown.buttons, ['send-anyway', 'cancel']);
});

/* --- slow answer: Checking card first, then the BLOCK card --- */

test('T63c slow answer shows Checking then the result card', async () => {
  const { doc, sent, fixture } = loadTestpage(
    '127.0.0.1', baseRoutes(BLOCK_REDACT, 10000));
  const { editor, btn, pageSent } = fixture;
  editor.value = MSG;
  editor.focus();
  await sleep(650); // debounce + scan round trip
  const before = redactCount(sent);
  doc._dispatch(new MiniEvent('click', { bubbles: true }), btn);
  await sleep(150);
  let card = cardState(doc);
  assert.equal(card.display, 'block', 'Checking card must show at once, got ' + JSON.stringify(card));
  assert.match(card.title, /Checking/);
  assert.equal(card.tone, 'review', 'Checking card is amber, got ' + JSON.stringify(card));
  assert.match(card.body, /0s/);
  await sleep(1500);
  card = cardState(doc);
  assert.match(card.title, /Checking/, 'still waiting at 1.5 s, got ' + JSON.stringify(card));
  assert.match(card.body, /1s/, 'elapsed seconds update, got ' + JSON.stringify(card));
  await sleep(9000); // slow answer lands at ~10 s
  assert.equal(redactCount(sent) - before, 1, 'one send = one check');
  card = cardState(doc);
  assert.equal(card.display, 'block');
  assert.match(card.title, /Blocked/);
  assert.match(card.body, /not sent/);
  assert.deepEqual(pageSent, [], 'BLOCK verdict must not send');
});

/* --- network error: only then is it "not running" --- */

test('T63c network error shows not running', async () => {
  const routes = baseRoutes(() => ({ ok: false, error: 'fetch failed' }), 1200);
  const { doc, sent, fixture } = loadTestpage('127.0.0.1', routes);
  const { editor, btn, pageSent } = fixture;
  editor.value = MSG;
  editor.focus();
  await sleep(650);
  doc._dispatch(new MiniEvent('click', { bubbles: true }), btn);
  await sleep(150);
  assert.match(cardState(doc).title, /Checking/);
  await sleep(1500);
  const card = cardState(doc);
  assert.match(card.title, /isn't running/, 'got ' + JSON.stringify(card));
  assert.match(card.body, /not sent|Nothing was sent/);
  assert.equal(redactCount(sent), 1, 'one send = one check');
  assert.deepEqual(pageSent, [], 'failed check must not send');
});

/* --- 401: not paired, never "isn't running" --- */

test('T63c 401 shows not paired', async () => {
  const routes = baseRoutes(
    () => ({ ok: false, status: 401, error: 'app returned HTTP 401' }), 1200);
  const { doc, sent, fixture } = loadTestpage('127.0.0.1', routes);
  const { editor, btn, pageSent } = fixture;
  editor.value = MSG;
  editor.focus();
  await sleep(650);
  doc._dispatch(new MiniEvent('click', { bubbles: true }), btn);
  await sleep(150);
  assert.match(cardState(doc).title, /Checking/);
  await sleep(1500);
  const card = cardState(doc);
  assert.match(card.title, /Not paired/, 'got ' + JSON.stringify(card));
  assert.match(card.body, /paste the pairing token/);
  assert.match(card.body, /Nothing was sent/);
  assert.doesNotMatch(card.title, /isn't running/);
  assert.equal(redactCount(sent), 1, 'one send = one check');
  assert.deepEqual(pageSent, [], 'failed check must not send');
});

/* --- 45 s silence: retry card, Retry sends a new check --- */

test('T63c timeout shows a retry card and Retry rechecks', async () => {
  const routes = baseRoutes(BLOCK_REDACT, 30);
  routes.SF_REDACT = { hang: true };
  const { doc, sent, fixture } = loadTestpage('127.0.0.1', routes);
  const { editor, btn, pageSent } = fixture;
  editor.value = MSG;
  editor.focus();
  await sleep(650);
  doc._dispatch(new MiniEvent('click', { bubbles: true }), btn);
  await sleep(400);
  assert.match(cardState(doc).title, /Checking/);
  assert.deepEqual(pageSent, [], 'hung check must not send');
  await sleep(46000); // past the 45 s send-check timeout
  let card = cardState(doc);
  assert.equal(card.display, 'block', 'retry card must appear, got ' + JSON.stringify(card));
  assert.match(card.title, /Still no answer/, 'got ' + JSON.stringify(card));
  assert.match(card.body, /nothing was sent/);
  assert.deepEqual(card.buttons, ['Retry', 'Cancel']);
  assert.deepEqual(pageSent, [], 'timed-out check must not send');
  // Retry runs a brand-new check with its own Checking card.
  const beforeRetry = redactCount(sent);
  const retryBtn = cardButtons(doc).find((b) => b.textContent === 'Retry');
  assert.ok(retryBtn, 'Retry button must exist');
  retryBtn.click();
  await sleep(400);
  assert.equal(redactCount(sent) - beforeRetry, 1, 'Retry must run a new check');
  card = cardState(doc);
  assert.match(card.title, /Checking/, 'Retry shows Checking again, got ' + JSON.stringify(card));
  assert.deepEqual(pageSent, [], 'retry must not send while its check hangs');
});
