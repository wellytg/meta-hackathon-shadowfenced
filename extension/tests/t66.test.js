'use strict';

/* T66: a full identity bundle is a hard BLOCK with a clear "why" card.
 * The block card reads "Blocked — not sent", carries the server why line
 * in plain words (never values), and keeps the found pieces as label-only
 * chips. Run with: node --test extension/tests/
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const logic = require('../src/logic.js');

const WHY =
  'Together, a name, date of birth, home address, phone number and email ' +
  'identify one real person and where to find them. Each is personal data; ' +
  'combined they enable identity theft, stalking and fraud.';

const COMBINATION = ['name', 'dob', 'street', 'postcode', 'phone', 'email'];

test('T66 normalizeRedact keeps a BLOCK bundle reason and chips', () => {
  const norm = logic.normalizeRedact({
    decision: 'BLOCK',
    notes: WHY,
    safe_text: '',
    combination: COMBINATION,
  });
  assert.equal(norm.action, 'BLOCK');
  assert.equal(norm.reason, WHY);
  assert.deepEqual(norm.combination, COMBINATION);
  for (const raw of ['Priya Ramanathan', '07700', '900461', 'Willow Crescent']) {
    assert.ok(!norm.reason.includes(raw), 'why line leaks ' + raw);
  }
});

test('T66 failSafeDecision preserves the BLOCK bundle through the gate', () => {
  const d = logic.failSafeDecision({
    ok: true,
    decision: logic.normalizeRedact({
      decision: 'BLOCK',
      notes: WHY,
      safe_text: '',
      combination: COMBINATION,
    }),
  });
  assert.equal(d.action, 'BLOCK');
  assert.equal(d.reason, WHY);
  assert.deepEqual(d.combination, COMBINATION);
});

test('T66 capture guard shows the Blocked card with the why line', () => {
  const card = logic.captureGuardCard({
    ok: true,
    decision: {
      action: 'BLOCK',
      reason: WHY,
      combination: COMBINATION,
    },
    text: 'x',
  });
  assert.equal(card.send, false);
  assert.equal(card.title, 'Blocked \u2014 not sent');
  assert.equal(card.body, WHY);
  assert.deepEqual(card.buttons, ['ok']);
  for (const raw of ['Priya Ramanathan', '07700', '900461', 'Willow Crescent']) {
    assert.ok(!card.body.includes(raw), 'card leaks ' + raw);
  }
});
