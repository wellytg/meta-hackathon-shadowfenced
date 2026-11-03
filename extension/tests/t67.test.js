'use strict';

/* T67 file guard: interception table, allow-once by hash, fail closed,
 * secrets never overridable.
 * Run with: node --test extension/tests/
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const logic = require('../src/logic.js');

const HASH_A = 'a'.repeat(64);
const HASH_B = 'b'.repeat(64);

/* --- interception decision table --- */

test('T67 change/drop/paste with files are intercepted', () => {
  for (const kind of ['change', 'drop', 'paste']) {
    assert.equal(
      logic.shouldInterceptFile({ kind, hasFiles: true, allowed: false }),
      true, kind);
  }
});

test('T67 dragover is never intercepted (only preventDefaulted)', () => {
  assert.equal(
    logic.shouldInterceptFile({ kind: 'dragover', hasFiles: true, allowed: false }),
    false);
});

test('T67 allow-once passes and empty events are left alone', () => {
  assert.equal(
    logic.shouldInterceptFile({ kind: 'change', hasFiles: true, allowed: true }),
    false);
  assert.equal(
    logic.shouldInterceptFile({ kind: 'drop', hasFiles: true, allowed: true }),
    false);
  assert.equal(
    logic.shouldInterceptFile({ kind: 'paste', hasFiles: true, allowed: true }),
    false);
  for (const kind of ['change', 'drop', 'paste']) {
    assert.equal(
      logic.shouldInterceptFile({ kind, hasFiles: false, allowed: false }),
      false, kind);
  }
});

test('T67 unknown kinds and bad input never intercept', () => {
  for (const kind of ['click', 'keydown', '', null, undefined]) {
    assert.equal(
      logic.shouldInterceptFile({ kind, hasFiles: true, allowed: false }),
      false, String(kind));
  }
  assert.equal(logic.shouldInterceptFile(null), false);
  assert.equal(logic.shouldInterceptFile({}), false);
});

/* --- allow-once by hash: one use, 2-minute expiry --- */

test('T67 allow-once TTL is 2 minutes', () => {
  assert.equal(logic.ALLOW_ONCE_TTL_MS, 120000);
});

test('T67 allow-once pass works exactly once', () => {
  const store = logic.allowOnceRemember({}, HASH_A, 1000000);
  assert.equal(logic.allowOnceConsume(store, HASH_A, 1000000 + 1000), true);
  assert.equal(logic.allowOnceConsume(store, HASH_A, 1000000 + 2000), false);
});

test('T67 allow-once expires after 2 minutes', () => {
  const store = logic.allowOnceRemember({}, HASH_A, 1000000);
  assert.equal(logic.allowOnceConsume(store, HASH_A, 1000000 + 119999), true);
  const store2 = logic.allowOnceRemember({}, HASH_B, 1000000);
  assert.equal(logic.allowOnceConsume(store2, HASH_B, 1000000 + 120000), false);
  assert.equal(logic.allowOnceConsume(store2, HASH_B, 1000000 + 3600000), false);
});

test('T67 allow-once ignores unknown and invalid hashes', () => {
  const store = logic.allowOnceRemember({}, HASH_A, 1000000);
  assert.equal(logic.allowOnceConsume(store, HASH_B, 1000000 + 1000), false);
  assert.equal(logic.allowOnceConsume(store, '', 1000000 + 1000), false);
  assert.equal(logic.allowOnceConsume({}, HASH_A, 1000000 + 1000), false);
  // Invalid hashes are never stored.
  const bad = logic.allowOnceRemember({}, 'not-a-hash', 1000000);
  assert.deepEqual(bad, {});
  const upper = logic.allowOnceRemember({}, HASH_A.toUpperCase(), 1000000);
  assert.equal(logic.allowOnceConsume(upper, HASH_A, 1000000 + 1000), true);
});

/* --- fail closed when the app is down --- */

test('T67 app-down card blocks with no override', () => {
  for (const input of [null, undefined, {}, { ok: false },
                       { ok: false, decision: { action: 'ALLOW' } }]) {
    const card = logic.fileScanCard(input);
    assert.equal(card.tone, 'block');
    assert.equal(card.title, "Not checked \u2014 Shadowfenced app isn't running");
    assert.match(card.body, /stays blocked/);
    assert.match(card.body, /Nothing was uploaded/);
    assert.deepEqual(card.buttons, ['ok']);
  }
});

test('T67 not-paired card names the token step', () => {
  for (const status of [401, 403]) {
    const card = logic.fileScanCard({ ok: false, status });
    assert.equal(card.title, 'Not paired');
    assert.match(card.body, /pairing token/);
    assert.deepEqual(card.buttons, ['ok']);
  }
});

/* --- result cards --- */

test('T67 clean card offers attach-now', () => {
  const card = logic.fileScanCard({
    ok: true, decision: 'CLEAN', fileName: 'notes.txt',
  });
  assert.equal(card.tone, 'allow');
  assert.equal(card.title, '\u2713 Checked \u00b7 nothing sensitive in notes.txt');
  assert.deepEqual(card.buttons, ['attach-now']);
});

test('T67 blocked card carries chips and a redacted copy', () => {
  const card = logic.fileScanCard({
    ok: true, decision: 'BLOCK', fileName: 'team.txt',
    kinds: [{ label: 'email address', count: 12 }, { label: 'AWS API key', count: 1 }],
    why: 'Found secrets. Nothing was uploaded.',
    has_secret: true, overridable: false,
  });
  assert.equal(card.tone, 'block');
  assert.equal(card.title, 'Blocked \u2014 not uploaded');
  assert.deepEqual(card.chips, ['email address \u00d712', 'AWS API key \u00d71']);
  assert.ok(card.buttons.indexOf('redacted-copy') !== -1);
  assert.ok(card.buttons.indexOf('cancel') !== -1);
});

test('T67 secrets are never overridable', () => {
  // Even if the server ever claimed otherwise, a secret stays blocked.
  for (const extra of [{ has_secret: true, overridable: true },
                       { identity_bundle: true, overridable: true },
                       { has_secret: true, identity_bundle: true, overridable: true }]) {
    const card = logic.fileScanCard(Object.assign({
      ok: true, decision: 'BLOCK', fileName: 'd.txt',
      kinds: [], why: 'why',
    }, extra));
    assert.ok(card.buttons.indexOf('upload-anyway') === -1,
      'override offered for ' + JSON.stringify(extra));
  }
});

test('T67 personal-data-only may offer upload anyway', () => {
  const card = logic.fileScanCard({
    ok: true, decision: 'BLOCK', fileName: 'team.txt',
    kinds: [{ label: 'email address', count: 2 }],
    why: 'Found 2 email addresses. Nothing was uploaded.',
    has_secret: false, identity_bundle: false, overridable: true,
  });
  assert.ok(card.buttons.indexOf('upload-anyway') !== -1);
  assert.ok(card.buttons.indexOf('redacted-copy') !== -1);
});

test('T67 unscannable offers only an unchecked upload behind confirm', () => {
  const card = logic.fileScanCard({
    ok: true, decision: 'UNSCANNABLE', fileName: 'x.bin',
    why: "This file type can't be checked. Nothing was uploaded.",
  });
  assert.equal(card.tone, 'block');
  assert.equal(card.title, "Blocked \u2014 couldn't check this file");
  assert.deepEqual(card.buttons, ['upload-unchecked', 'cancel']);
});

test('T67 confirm cards gate both overrides', () => {
  const anyway = logic.fileConfirmCard('upload-anyway');
  assert.equal(anyway.title, 'Upload anyway?');
  assert.deepEqual(anyway.buttons, ['confirm-upload', 'cancel']);
  const unchecked = logic.fileConfirmCard('upload-unchecked');
  assert.equal(unchecked.title, 'Upload without checking?');
  assert.deepEqual(unchecked.buttons, ['confirm-upload', 'cancel']);
});

test('T67 deep-check button appears only when available', () => {
  const withDeep = logic.fileScanCard({
    ok: true, decision: 'CLEAN', fileName: 'n.txt',
    deep_available: true, deep_estimate_s: 300,
  });
  assert.deepEqual(withDeep.buttons, ['attach-now', 'deep-check']);
  assert.equal(logic.deepCheckMinutes(300), 5);
  assert.equal(logic.deepCheckMinutes(0), 1);
  const without = logic.fileScanCard({
    ok: true, decision: 'CLEAN', fileName: 'n.txt', deep_available: false,
  });
  assert.deepEqual(without.buttons, ['attach-now']);
});
