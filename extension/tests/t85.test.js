'use strict';

/* T85: Grok (grok.com) adapter — selectAdapter picks grok for grok.com
 * (exact host, www subdomain, any case) and the editor/send-button
 * selectors find the live-page composer (a textarea with
 * aria-label "Ask Grok anything" inside a form) in a small DOM fixture.
 * The selectors below mirror the Nov 4 DevTools capture; the adapter is
 * not yet tested live. Run with: node --test extension/tests/
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const logic = require('../src/logic.js');
const adapters = require('../src/adapters.js');

const ADAPTERS = adapters.ADAPTERS;
const grok = ADAPTERS.find((a) => a.id === 'grok');

/* --- tiny fixture DOM with a real selector subset: tag,
 * [attr="v"], [attr*="v" i], and one descendant combinator. --- */

function makeEl(tag, attrs, children) {
  const node = {
    tag: String(tag).toLowerCase(),
    attrs: {},
    children: [],
    parent: null,
  };
  for (const k of Object.keys(attrs || {})) {
    node.attrs[k.toLowerCase()] = String(attrs[k]);
  }
  for (const c of children || []) {
    c.parent = node;
    node.children.push(c);
  }
  return node;
}

function matchCompound(src, node) {
  let rest = src.trim();
  const tagM = rest.match(/^([a-zA-Z][a-zA-Z0-9-]*|\*)/);
  const tag = tagM ? tagM[1].toLowerCase() : null;
  if (tagM) rest = rest.slice(tagM[0].length);
  if (tag && tag !== '*' && node.tag !== tag) return false;
  const attrRe = /\[([^\]]*)\]/g;
  let m;
  while ((m = attrRe.exec(rest))) {
    const a = m[1].match(/^\s*([a-zA-Z0-9_-]+)\s*(?:(=|\*=)\s*"([^"]*)"\s*(i)?)?\s*$/);
    assert.ok(a, 'unsupported attr selector: [' + m[1] + ']');
    const v = node.attrs[a[1].toLowerCase()];
    if (v === undefined) return false;
    if (!a[2]) continue;
    const hay = a[4] ? v.toLowerCase() : v;
    const ndl = a[4] ? a[3].toLowerCase() : a[3];
    if (a[2] === '=' && hay !== ndl) return false;
    if (a[2] === '*=' && !hay.includes(ndl)) return false;
  }
  return true;
}

function matchesSelector(sel, node) {
  const parts = sel.trim().split(/\s+/);
  if (!matchCompound(parts[parts.length - 1], node)) return false;
  let scope = node.parent;
  for (let p = parts.length - 2; p >= 0; p--) {
    let found = false;
    while (scope) {
      if (matchCompound(parts[p], scope)) { found = true; scope = scope.parent; break; }
      scope = scope.parent;
    }
    if (!found) return false;
  }
  return true;
}

function queryAll(roots, sel) {
  const out = [];
  (function walk(node) {
    if (matchesSelector(sel, node)) out.push(node);
    for (const c of node.children) walk(c);
  })({ children: roots });
  return out;
}

/* Mirror of the Nov 4 live page: textarea composer with
 * aria-label "Ask Grok anything" inside a form, round submit button
 * at the right of the composer, replies in the main column. */
function grokPage() {
  const textarea = makeEl('textarea', {
    dir: 'auto',
    'aria-label': 'Ask Grok anything',
    'aria-autocomplete': 'list',
    class: 'w-full px-2 prose',
  });
  const sendBtn = makeEl('button', { type: 'submit', 'aria-label': 'Submit' });
  const form = makeEl('form', {}, [textarea, sendBtn]);
  const reply = makeEl('article', {}, []);
  const main = makeEl('main', {}, [reply]);
  const doc = {
    querySelector(sel) {
      const hit = queryAll([form, main], sel);
      return hit.length ? hit[0] : null;
    },
  };
  return { doc, form, textarea, sendBtn, main, reply };
}

test('T85 grok adapter exists with the standard shape', () => {
  assert.ok(grok, 'adapters.js has no grok adapter');
  assert.deepEqual(grok.hosts, ['grok.com']);
  for (const key of ['editor', 'sendButton', 'replyArea', 'attachment']) {
    assert.ok(Array.isArray(grok[key]) && grok[key].length > 0,
      'grok adapter needs a non-empty ' + key);
  }
  assert.ok(grok.editor.includes('textarea[aria-label*="Ask Grok" i]'));
  assert.ok(grok.sendButton.includes('form button[type="submit"]'));
  assert.ok(grok.attachment.includes('input[type="file"]'));
});

test('T85 selectAdapter picks grok for grok.com', () => {
  assert.equal(logic.selectAdapter('grok.com', ADAPTERS).id, 'grok');
  assert.equal(logic.selectAdapter('www.grok.com', ADAPTERS).id, 'grok');
  assert.equal(logic.selectAdapter('Grok.COM', ADAPTERS).id, 'grok');
  assert.equal(adapters.selectAdapter('grok.com').id, 'grok');
  assert.equal(adapters.selectAdapter('www.grok.com').id, 'grok');
});

test('T85 grok adapter does not steal other hosts', () => {
  assert.equal(logic.selectAdapter('chatgpt.com', ADAPTERS).id, 'chatgpt');
  assert.equal(logic.selectAdapter('claude.ai', ADAPTERS).id, 'claude');
  assert.equal(logic.selectAdapter('gemini.google.com', ADAPTERS).id, 'gemini');
  assert.equal(logic.selectAdapter('meta.ai', ADAPTERS).id, 'meta');
  assert.equal(logic.selectAdapter('example.com', ADAPTERS), null);
  assert.equal(logic.selectAdapter('grok.com.evil.com', ADAPTERS), null);
});

test('T85 grok editor selectors find the textarea in the fixture', () => {
  const page = grokPage();
  const found = adapters.queryFirst(grok.editor, page.doc);
  assert.equal(found, page.textarea);
  assert.equal(found.attrs['aria-label'], 'Ask Grok anything');
});

test('T85 grok send-button and reply selectors match the fixture', () => {
  const page = grokPage();
  assert.equal(adapters.queryFirst(grok.sendButton, page.doc), page.sendBtn);
  const replyHit = queryAll([page.form, page.main], grok.replyArea[1]);
  assert.ok(replyHit.includes(page.reply), 'reply selector misses the conversation column');
});
