'use strict';

/* T67c: no "Upload anyway" for secrets, canaries, identity bundles or
 * financial identifiers; chips stay separate items; deep-check label
 * shows the server estimate ("about 15 s", "about 2 min"); file cards
 * are wide with wrapped buttons, bordered chips, and filename/why
 * blocks.
 * Run with: node --test extension/tests/
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const logic = require('../src/logic.js');
const CONTENT_SRC = fs.readFileSync(
  path.join(__dirname, '..', 'src', 'content.js'), 'utf8');

/* --- upload-anyway visibility table --- */

function blockedCard(extra) {
  return logic.fileScanCard(Object.assign({
    ok: true, decision: 'BLOCK', fileName: 'f.txt',
    kinds: [], why: 'why. Nothing was uploaded.',
    has_secret: false, identity_bundle: false,
    has_financial: false, overridable: true,
  }, extra));
}

test('T67c financial files never offer upload anyway', () => {
  const financialKinds = [
    [{ label: 'account number', count: 2 }],
    [{ label: 'sort code', count: 1 }],
    [{ label: 'card number', count: 1 }],
    [{ label: 'IBAN', count: 1 }],
    [{ label: 'routing number', count: 1 }],
    [{ label: 'SWIFT code', count: 1 }],
  ];
  for (const kinds of financialKinds) {
    const card = blockedCard({ kinds, overridable: true });
    assert.ok(card.buttons.indexOf('upload-anyway') === -1,
      'override offered for ' + JSON.stringify(kinds));
    assert.ok(card.buttons.indexOf('redacted-copy') !== -1);
    assert.ok(card.buttons.indexOf('cancel') !== -1);
  }
  // Server flag alone is enough, even with empty kinds.
  const flagged = blockedCard({ has_financial: true, overridable: true });
  assert.ok(flagged.buttons.indexOf('upload-anyway') === -1);
});

test('T67c secrets, canaries and bundles never offer upload anyway', () => {
  for (const extra of [
    { has_secret: true, overridable: true },
    { identity_bundle: true, overridable: true },
    { kinds: [{ label: 'canary token', count: 1 }], overridable: true },
    { kinds: [{ label: 'AWS API key', count: 1 }], overridable: true },
    { kinds: [{ label: 'secret', count: 1 }], overridable: true },
    { kinds: [{ label: 'full identity bundle', count: 1 }],
      overridable: true },
  ]) {
    const card = blockedCard(extra);
    assert.ok(card.buttons.indexOf('upload-anyway') === -1,
      'override offered for ' + JSON.stringify(extra));
  }
});

test('T67c lighter personal data keeps upload anyway behind confirm', () => {
  const card = blockedCard({
    kinds: [
      { label: 'person name', count: 2 },
      { label: 'email address', count: 2 },
      { label: 'phone number', count: 2 },
    ],
    overridable: true,
  });
  assert.ok(card.buttons.indexOf('upload-anyway') !== -1);
  assert.ok(card.buttons.indexOf('redacted-copy') !== -1);
  const confirm = logic.fileConfirmCard('upload-anyway');
  assert.equal(confirm.title, 'Upload anyway?');
  assert.deepEqual(confirm.buttons, ['confirm-upload', 'cancel']);
});

test('T67c unscannable keeps its unchecked upload behind confirm', () => {
  const card = logic.fileScanCard({
    ok: true, decision: 'UNSCANNABLE', fileName: 'x.bin',
    why: "This file type can't be checked. Nothing was uploaded.",
  });
  assert.deepEqual(card.buttons, ['upload-unchecked', 'cancel']);
});

/* --- chips stay separate --- */

test('T67c card model chips are separate items', () => {
  const card = blockedCard({
    kinds: [
      { label: 'person name', count: 6 },
      { label: 'email address', count: 6 },
      { label: 'phone number', count: 6 },
    ],
  });
  assert.ok(Array.isArray(card.chips));
  assert.deepEqual(card.chips, [
    'person name \u00d76',
    'email address \u00d76',
    'phone number \u00d76',
  ]);
  assert.ok(!card.chips.some((c) => /name\u00d76email/.test(c)),
    'chips must not run together into one string');
});

/* --- deep-check estimate label --- */

test('T67c deep estimate shows server seconds and minutes', () => {
  assert.equal(logic.deepCheckEstimateLabel(15), 'about 15 s');
  assert.equal(logic.deepCheckEstimateLabel(1), 'about 1 s');
  assert.equal(logic.deepCheckEstimateLabel(59), 'about 59 s');
  assert.equal(logic.deepCheckEstimateLabel(60), 'about 1 min');
  assert.equal(logic.deepCheckEstimateLabel(120), 'about 2 min');
  assert.equal(logic.deepCheckEstimateLabel(140), 'about 2 min');
});

/* --- file card layout markers in content.js --- */

test('T67c file cards are wide with wrapped buttons and chip boxes', () => {
  for (const marker of [
    '.sf-card[data-file="1"]{width:440px;max-width:92vw;}',
    '.sf-card[data-file="1"] .sf-cbtns{flex-wrap:wrap;}',
    '.sf-chips{display:flex;flex-wrap:wrap;gap:6px;',
    '.sf-chip{border:2px solid #1a1a1a;',
    '.sf-fname{font-weight:700;',
    '.sf-why{',
    "card.setAttribute('data-file'",
    'fgDeepEstimate(deep.estimate_seconds)',
    'has_financial: data.has_financial',
  ]) {
    assert.ok(CONTENT_SRC.indexOf(marker) !== -1,
      'content.js missing ' + JSON.stringify(marker));
  }
});
