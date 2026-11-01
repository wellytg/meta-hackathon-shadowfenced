'use strict';

/* Shadowfenced extension logic tests (T36): span-to-range mapping,
 * adapter selection, and the fail-safe send decision table.
 * Run with: node --test extension/tests/
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const logic = require('../src/logic.js');

const ADAPTERS = [
  { id: 'chatgpt', hosts: ['chatgpt.com'] },
  { id: 'claude', hosts: ['claude.ai'] },
  { id: 'gemini', hosts: ['gemini.google.com'] },
  { id: 'meta', hosts: ['meta.ai'] },
];

test('selectAdapter matches exact hosts', () => {
  assert.equal(logic.selectAdapter('chatgpt.com', ADAPTERS).id, 'chatgpt');
  assert.equal(logic.selectAdapter('claude.ai', ADAPTERS).id, 'claude');
  assert.equal(logic.selectAdapter('gemini.google.com', ADAPTERS).id, 'gemini');
  assert.equal(logic.selectAdapter('meta.ai', ADAPTERS).id, 'meta');
});

test('selectAdapter matches subdomains and ignores case', () => {
  assert.equal(logic.selectAdapter('www.meta.ai', ADAPTERS).id, 'meta');
  assert.equal(logic.selectAdapter('ChatGPT.COM', ADAPTERS).id, 'chatgpt');
});

test('selectAdapter accepts a dict of adapters', () => {
  const dict = { a: ADAPTERS[0], b: ADAPTERS[1] };
  assert.equal(logic.selectAdapter('claude.ai', dict).id, 'claude');
});

test('selectAdapter no-match returns null and does nothing', () => {
  assert.equal(logic.selectAdapter('example.com', ADAPTERS), null);
  assert.equal(logic.selectAdapter('chatgpt.com.evil.com', ADAPTERS), null);
  assert.equal(logic.selectAdapter('', ADAPTERS), null);
  assert.equal(logic.selectAdapter(null, ADAPTERS), null);
  assert.equal(logic.selectAdapter('meta.ai', null), null);
  assert.equal(logic.selectAdapter('meta.ai', []), null);
});

test('spansToRanges clamps, drops empties, sorts and merges', () => {
  assert.deepEqual(
    logic.spansToRanges(
      [{ start: 10, end: 4 }, { start: 0, end: 3 }, { start: 2, end: 6 }],
      20
    ),
    [{ start: 0, end: 6 }]
  );
  assert.deepEqual(
    logic.spansToRanges([{ start: -5, end: 4 }, { start: 18, end: 99 }], 20),
    [{ start: 0, end: 4 }, { start: 18, end: 20 }]
  );
  assert.deepEqual(
    logic.spansToRanges([{ start: 5, end: 5 }, { start: 7, end: 7 }], 20),
    []
  );
  // Adjacent ranges merge.
  assert.deepEqual(
    logic.spansToRanges([{ start: 0, end: 3 }, { start: 3, end: 6 }], 20),
    [{ start: 0, end: 6 }]
  );
});

test('spansToRanges rejects bad input', () => {
  assert.deepEqual(logic.spansToRanges([], 20), []);
  assert.deepEqual(logic.spansToRanges(null, 20), []);
  assert.deepEqual(logic.spansToRanges([{ start: 0, end: 3 }], 0), []);
});

test('failSafeDecision blocks every failure with the not-running message', () => {
  for (const input of [null, undefined, {}, { ok: false }, { ok: false, decision: { action: 'ALLOW' } }]) {
    const d = logic.failSafeDecision(input);
    assert.equal(d.action, 'BLOCK');
    assert.match(d.reason, /isn't running/);
  }
});

test('failSafeDecision maps BLOCK/REDACT/HOLD/WARN/ALLOW', () => {
  assert.deepEqual(logic.failSafeDecision({ ok: true, decision: { action: 'BLOCK', reason: 'no' } }),
    { action: 'BLOCK', reason: 'no' });
  assert.deepEqual(
    logic.failSafeDecision({ ok: true, decision: { action: 'REDACT', safe_text: 'a [PII_EMAIL_1]', count: 1 } }),
    { action: 'REDACT', safe_text: 'a [PII_EMAIL_1]', count: 1 });
  assert.equal(logic.failSafeDecision({ ok: true, decision: { action: 'HOLD', reason: 'review' } }).action, 'HOLD');
  assert.equal(logic.failSafeDecision({ ok: true, decision: { action: 'WARN', reason: 'careful' } }).action, 'HOLD');
  assert.deepEqual(logic.failSafeDecision({ ok: true, decision: { action: 'ALLOW' } }),
    { action: 'ALLOW' });
});

test('failSafeDecision blocks unknown actions', () => {
  for (const action of ['DELETE', '', null, undefined]) {
    const d = logic.failSafeDecision({ ok: true, decision: { action } });
    assert.equal(d.action, 'BLOCK');
    assert.match(d.reason, /isn't running/);
  }
  const missing = logic.failSafeDecision({ ok: true });
  assert.equal(missing.action, 'BLOCK');
});
