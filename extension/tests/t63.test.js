'use strict';

/* T63 live-test regressions: adapter selectors against the captured DOM
 * snippets (docs/muse-briefs/T63_domdump.json), input-element support,
 * fail-visible state, capture-guard decisions, app-down held cards,
 * attachment counting, hover-card focus, and the popup health mapping.
 * Run with: node --test extension/tests/
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const logic = require('../src/logic.js');
const { ADAPTERS } = require('../src/adapters.js');
const DOMDUMP = require('../../docs/muse-briefs/T63_domdump.json');

/* --- tiny CSS-subset matcher: tag, #id, .class, [attr] with = *= ^= $=
 * and an optional i flag, :not(), and descendant combinators. --- */

function parseAttr(inner) {
  const m = inner.match(/^\s*([a-zA-Z0-9_-]+)\s*(?:(=|\*=|\^=|\$=)\s*"([^"]*)"\s*(i)?)?\s*$/);
  if (!m) throw new Error('unsupported attr selector: [' + inner + ']');
  return { name: m[1].toLowerCase(), op: m[2] || null, value: m[3] || '', ci: !!m[4] };
}

function parseCompound(src) {
  const nots = [];
  const s = src.replace(/:not\(([^()]*)\)/g, (_, inner) => { nots.push(inner.trim()); return ''; });
  const c = { tag: null, id: null, classes: [], attrs: [], nots };
  const tagM = s.match(/^([a-zA-Z][a-zA-Z0-9-]*|\*)/);
  if (tagM) c.tag = tagM[1].toLowerCase();
  const idM = s.match(/#([a-zA-Z0-9_-]+)/);
  if (idM) c.id = idM[1];
  const classRe = /\.([a-zA-Z0-9_-]+)/g;
  let m;
  while ((m = classRe.exec(s))) c.classes.push(m[1]);
  const attrRe = /\[([^\]]*)\]/g;
  while ((m = attrRe.exec(s))) c.attrs.push(parseAttr(m[1]));
  return c;
}

function matchCompound(c, e) {
  if (c.tag && c.tag !== '*' && (e.tag || '').toLowerCase() !== c.tag) return false;
  if (c.id && e.id !== c.id) return false;
  for (const cls of c.classes) {
    if (!e.classes.includes(cls)) return false;
  }
  for (const a of c.attrs) {
    const v = e.attrs[a.name];
    if (v === undefined) return false;
    if (a.op === null) continue;
    const hay = a.ci ? v.toLowerCase() : v;
    const ndl = a.ci ? a.value.toLowerCase() : a.value;
    if (a.op === '=' && hay !== ndl) return false;
    if (a.op === '*=' && !hay.includes(ndl)) return false;
    if (a.op === '^=' && !hay.startsWith(ndl)) return false;
    if (a.op === '$=' && !hay.endsWith(ndl)) return false;
  }
  for (const n of c.nots) {
    if (matchCompound(parseCompound(n), e)) return false;
  }
  return true;
}

/* Split a selector on descendant combinators without breaking quoted
 * attribute values such as [aria-label="Send message"]. */
function splitSelector(sel) {
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

function matchesSelector(sel, e) {
  const parts = splitSelector(sel).map(parseCompound);
  const chain = [e, ...((e.ancestors) || [])];
  if (!matchCompound(parts[parts.length - 1], chain[0])) return false;
  let depth = 1;
  for (let p = parts.length - 2; p >= 0; p--) {
    let found = false;
    while (depth < chain.length) {
      if (matchCompound(parts[p], chain[depth])) { found = true; depth++; break; }
      depth++;
    }
    if (!found) return false;
  }
  return true;
}

function el(tag, opts) {
  opts = opts || {};
  const attrs = {};
  for (const k of Object.keys(opts.attrs || {})) attrs[k.toLowerCase()] = String(opts.attrs[k]);
  return {
    tag: (tag || '').toLowerCase(),
    id: opts.id || '',
    classes: (opts.className || '').split(/\s+/).filter(Boolean),
    attrs,
    ancestors: opts.ancestors || [],
  };
}

function adapterMatches(adapter, kind, element) {
  return (adapter[kind] || []).filter((s) => {
    try { return matchesSelector(s, element); } catch (e) { return false; }
  });
}

function adapterById(id) {
  const a = ADAPTERS.find((x) => x.id === id);
  assert.ok(a, 'missing adapter ' + id);
  return a;
}

/* Active-element HTML from the domdump, parsed for tag/id/attributes. */
function parseActive(html) {
  const out = el((html.match(/^<\s*([a-zA-Z][a-zA-Z0-9-]*)/) || [])[1] || 'div');
  const idM = html.match(/\sid\s*=\s*"([^"]*)"/);
  if (idM) out.id = idM[1];
  const classM = html.match(/\sclass\s*=\s*"([^"]*)"/);
  if (classM) out.classes = classM[1].split(/\s+/).filter(Boolean);
  for (const name of ['aria-label', 'placeholder', 'type', 'contenteditable', 'role']) {
    const m = html.match(new RegExp('\\s' + name + '\\s*=\\s*"([^"]*)"'));
    if (m) out.attrs[name] = m[1];
  }
  return out;
}

test('T63 chatgpt: signed-out textarea composer is matched', () => {
  const a = adapterById('chatgpt');
  const active = parseActive(DOMDUMP.chatgpt.active);
  assert.equal(active.tag, 'textarea');
  assert.equal(active.id, 'mobile-composer-prompt');
  assert.match(active.attrs['aria-label'] || '', /Chat with ChatGPT/);
  const composer = el('textarea', {
    id: active.id,
    attrs: {
      'aria-label': active.attrs['aria-label'],
      placeholder: DOMDUMP.chatgpt.editables[0].ph,
    },
    ancestors: [el('form'), el('main')],
  });
  assert.ok(adapterMatches(a, 'editor', composer).length > 0,
    'no editor selector matches the signed-out composer');
  // The pre-T63 selector set missed it entirely (the live bug).
  const OLD = [
    '#prompt-textarea',
    'div[contenteditable="true"]#prompt-textarea',
    'form div[contenteditable="true"]',
    'main div[contenteditable="true"]',
  ];
  assert.deepEqual(OLD.filter((s) => matchesSelector(s, composer)), []);
  // The signed-in selectors are kept for the contenteditable composer.
  for (const s of OLD) assert.ok(a.editor.includes(s), 'lost signed-in selector ' + s);
});

test('T63 chatgpt: send button is matched', () => {
  const a = adapterById('chatgpt');
  assert.ok(DOMDUMP.chatgpt.buttons.some((b) => b.startsWith('Send message')),
    'domdump has no Send message button');
  const btn = el('button', {
    attrs: { type: 'submit', 'aria-label': 'Send message' },
    ancestors: [el('form')],
  });
  assert.ok(adapterMatches(a, 'sendButton', btn).includes('button[aria-label="Send message"]'));
});

test('T63 meta.ai: input composer and Send button are matched', () => {
  const a = adapterById('meta');
  assert.equal(DOMDUMP.metaai.editables[0].tag, 'INPUT');
  const composer = el('input', {
    attrs: { type: 'text', placeholder: DOMDUMP.metaai.editables[0].ph },
    ancestors: [el('form')],
  });
  assert.ok(adapterMatches(a, 'editor', composer).length > 0,
    'no editor selector matches the meta.ai input composer');
  const btn = el('button', { attrs: { type: 'submit', 'aria-label': 'Send' } });
  assert.ok(adapterMatches(a, 'sendButton', btn).length > 0,
    'no sendButton selector matches the meta.ai Send button');
});

test('T63 gemini: contenteditable composer and send are matched', () => {
  const a = adapterById('gemini');
  const active = parseActive(DOMDUMP.gemini.active);
  assert.equal(active.tag, 'div');
  assert.equal(active.attrs.contenteditable, 'true');
  const composer = el('div', {
    className: (active.classes || []).join(' '),
    attrs: { contenteditable: 'true', 'aria-label': active.attrs['aria-label'] || '' },
    ancestors: [el('rich-textarea')],
  });
  assert.ok(adapterMatches(a, 'editor', composer).length > 0,
    'no editor selector matches the Gemini composer');
  const btn = el('button', { attrs: { type: 'submit', 'aria-label': 'Send message' } });
  assert.ok(adapterMatches(a, 'sendButton', btn).length > 0,
    'no sendButton selector matches the Gemini send button');
});

test('T63 attachments: buttons never count, chips and files do', () => {
  for (const a of ADAPTERS) {
    for (const s of (a.attachment || [])) {
      assert.ok(!/upload/i.test(s), a.id + ' attachment selector matches buttons: ' + s);
    }
  }
  const uploadBtn = el('button', { attrs: { type: 'submit', 'aria-label': 'Upload & tools' } });
  const attachBtn = el('button', { attrs: { type: 'submit', 'aria-label': 'Add attachment' } });
  for (const a of ADAPTERS) {
    assert.deepEqual(adapterMatches(a, 'attachment', uploadBtn), [],
      a.id + ' matches the Upload button');
    assert.deepEqual(adapterMatches(a, 'attachment', attachBtn), [],
      a.id + ' matches the Add-attachment button');
  }
  assert.equal(logic.countsAsAttachment({ tagName: 'BUTTON', type: 'submit', files: null }), false);
  assert.equal(logic.countsAsAttachment({ tagName: 'INPUT', type: 'file', files: [] }), false);
  assert.equal(logic.countsAsAttachment({ tagName: 'INPUT', type: 'file', files: ['a.png'] }), true);
  assert.equal(logic.countsAsAttachment({ tagName: 'INPUT', type: 'text', files: null }), false);
  assert.equal(logic.countsAsAttachment({ tagName: 'DIV', type: '', files: null }), true);
  assert.equal(logic.countsAsAttachment(null), false);
});

test('T63 input elements count as editable, buttons and file inputs do not', () => {
  assert.equal(logic.isEditableElement({ tagName: 'INPUT', type: 'text' }), true);
  assert.equal(logic.isEditableElement({ tagName: 'INPUT', type: '' }), true);
  assert.equal(logic.isEditableElement({ tagName: 'TEXTAREA', type: '' }), true);
  assert.equal(logic.isEditableElement({ tagName: 'DIV', isContentEditable: true }), true);
  assert.equal(logic.isEditableElement({
    tagName: 'DIV',
    getAttribute: (n) => (n === 'role' ? 'textbox' : null),
  }), true);
  assert.equal(logic.isEditableElement({ tagName: 'INPUT', type: 'file' }), false);
  assert.equal(logic.isEditableElement({ tagName: 'INPUT', type: 'hidden' }), false);
  assert.equal(logic.isEditableElement({ tagName: 'BUTTON', type: 'submit' }), false);
  assert.equal(logic.isEditableElement(null), false);
});

test('T63 fail-visible: unprotected state, badge text, amber site row', () => {
  assert.equal(logic.protectionStatus({ supported: true, editorFound: false }), 'unprotected');
  assert.equal(logic.protectionStatus({ supported: true, editorFound: true }), 'protecting');
  assert.equal(logic.protectionStatus({ supported: false }), 'unsupported');
  assert.equal(logic.NOT_PROTECTING_TEXT, 'Shadowfenced: not protecting this page');
  const now = Date.now();
  assert.equal(logic.siteRowTone({ protecting: false, ts: now }, now), 'amber');
  assert.equal(logic.siteRowTone({ protecting: true, ts: now }, now), '');
  assert.equal(logic.siteRowTone({ protecting: false, ts: now - 600000 }, now), '');
  assert.equal(logic.siteRowTone(undefined, now), '');
});

test('T63 capture guard: blocks a key send with no adapter attached', () => {
  const base = {
    kind: 'enter', key: 'Enter', shift: false, composing: false,
    supported: true, adapterAttached: false, armed: false, sending: false,
    siteOn: true, activeEditable: true, onSendButton: false,
  };
  assert.equal(logic.guardShouldIntercept(base), true);
  assert.equal(logic.guardShouldIntercept({ ...base, shift: true }), false);
  assert.equal(logic.guardShouldIntercept({ ...base, key: 'a' }), false);
  assert.equal(logic.guardShouldIntercept({ ...base, supported: false }), false);
  assert.equal(logic.guardShouldIntercept({ ...base, adapterAttached: true }), false);
  assert.equal(logic.guardShouldIntercept({ ...base, activeEditable: false }), false);
  assert.equal(logic.guardShouldIntercept({ ...base, siteOn: false }), false);
  assert.equal(logic.guardShouldIntercept({ ...base, armed: true }), false);
  const click = {
    kind: 'click', supported: true, adapterAttached: false, armed: false,
    sending: false, siteOn: true, activeEditable: true, onSendButton: true,
  };
  assert.equal(logic.guardShouldIntercept(click), true);
  assert.equal(logic.guardShouldIntercept({ ...click, onSendButton: false }), false);
});

test('T63 app-down: held card offers Send anyway, never for secrets', () => {
  const held = logic.captureGuardCard({ ok: false, text: 'hello there' });
  assert.equal(held.send, false);
  assert.match(held.body, /message not sent/);
  assert.deepEqual(held.buttons, ['send-anyway', 'cancel']);
  // Key fragment is joined at runtime so no live-key shape is stored here.
  const key = ['AK', 'IA'].join('') + 'IOSFODNN7EXAMPLE';
  assert.equal(logic.looksLikeSecret('deploy with ' + key), true);
  const secretHeld = logic.captureGuardCard({ ok: false, text: 'deploy with ' + key });
  assert.equal(secretHeld.send, false);
  assert.deepEqual(secretHeld.buttons, ['ok']);
  assert.equal(logic.looksLikeSecret('hello there'), false);
});

test('T63 guard: BLOCK shows the Blocked card, other verdicts pass through', () => {
  const blocked = logic.captureGuardCard({
    ok: true, decision: { action: 'BLOCK', reason: 'no' }, text: 'x',
  });
  assert.equal(blocked.send, false);
  assert.equal(blocked.title, 'Blocked \u2014 not sent');
  assert.deepEqual(blocked.buttons, ['ok']);
  assert.deepEqual(
    logic.captureGuardCard({ ok: true, decision: { action: 'ALLOW' }, text: 'x' }),
    { send: true });
  assert.deepEqual(
    logic.captureGuardCard({ ok: true, decision: { action: 'HOLD', reason: 'r' }, text: 'x' }),
    { send: true });
});

test('T63 normalizeRedact maps the server payload onto send decisions', () => {
  assert.deepEqual(
    logic.normalizeRedact({ decision: 'BLOCK', notes: 'no', safe_text: '' }),
    { action: 'BLOCK', reason: 'no' });
  assert.deepEqual(
    logic.normalizeRedact({ decision: 'REDACT', safe_text: 'mail [PII_EMAIL_1] now', notes: 'n' }),
    { action: 'REDACT', safe_text: 'mail [PII_EMAIL_1] now', count: 1 });
  assert.equal(logic.normalizeRedact({ decision: 'HOLD', notes: 'review me' }).action, 'HOLD');
  assert.deepEqual(
    logic.normalizeRedact({ decision: 'ALLOW', safe_text: 'hi', notes: '' }),
    { action: 'ALLOW' });
  assert.deepEqual(logic.normalizeRedact({}), {});
  // End to end through the fail-safe table: a real BLOCK keeps its
  // reason, unknown shapes collapse to the not-running message.
  assert.equal(logic.failSafeDecision({
    ok: true, decision: logic.normalizeRedact({ decision: 'BLOCK', notes: 'no' }),
  }).reason, 'no');
  assert.match(logic.failSafeDecision({
    ok: true, decision: logic.normalizeRedact({}),
  }).reason, /isn't running/);
  assert.match(logic.failSafeDecision({ ok: false, decision: {} }).reason, /isn't running/);
});

test('T63 hover card never takes focus; notices still do', () => {
  assert.equal(logic.shouldFocusCard('hover'), false);
  assert.equal(logic.shouldFocusCard('notice'), true);
  assert.equal(logic.shouldFocusCard(undefined), true);
});

test('T63 popup status: Connected / Not paired / Not running', () => {
  assert.deepEqual(logic.healthToStatus({ ok: true, data: {} }),
    { text: 'Connected \u00b7 checking locally', tone: 'ok' });
  assert.deepEqual(logic.healthToStatus({ ok: false, status: 401 }),
    { text: 'Not paired \u2014 paste your token', tone: 'warn' });
  assert.deepEqual(logic.healthToStatus({ ok: false, status: 403 }),
    { text: 'Not paired \u2014 paste your token', tone: 'warn' });
  assert.deepEqual(logic.healthToStatus({ ok: false, error: 'app returned HTTP 404' }),
    { text: 'Not running \u2014 start Shadowfenced', tone: 'bad' });
  assert.deepEqual(logic.healthToStatus({ ok: false }),
    { text: 'Not running \u2014 start Shadowfenced', tone: 'bad' });
  assert.deepEqual(logic.healthToStatus(null),
    { text: 'Not running \u2014 start Shadowfenced', tone: 'bad' });
});

test('T63 testpage adapter still matches the local demo composer', () => {
  const a = adapterById('testpage');
  assert.ok(adapterMatches(a, 'editor', el('textarea', { id: 'composer-text' })).length > 0);
  assert.ok(adapterMatches(a, 'sendButton', el('button', { id: 'sendBtn' })).length > 0);
});
