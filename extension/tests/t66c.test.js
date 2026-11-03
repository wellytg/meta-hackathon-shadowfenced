'use strict';

/* T66c: the BLOCK bundle card keeps the found pieces as separate chip
 * labels (an array, never one run-together string) and the why line in
 * full (never truncated with an ellipsis). Run with:
 * node --test extension/tests/
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const logic = require('../src/logic.js');

const WHY =
  'Together, a name, date of birth, home address, phone number and email ' +
  'identify one real person and where to find them. Each is personal data; ' +
  'combined they enable identity theft, stalking and fraud.';

const COMBINATION = ['name', 'dob', 'street', 'postcode', 'phone', 'email'];

test('T66c BLOCK keeps bundle pieces as separate chip labels', () => {
  const norm = logic.normalizeRedact({
    decision: 'BLOCK',
    notes: WHY,
    safe_text: '',
    combination: COMBINATION,
  });
  assert.ok(Array.isArray(norm.combination), 'chips must stay separate elements');
  assert.deepEqual(norm.combination, COMBINATION);
  assert.ok(!norm.combination.some((c) => /date of birthstreet/.test(c)),
    'chips must not run together into one string');
});

test('T66c BLOCK why line is complete, never ellipsized', () => {
  const norm = logic.normalizeRedact({
    decision: 'BLOCK',
    notes: WHY,
    safe_text: '',
    combination: COMBINATION,
  });
  assert.equal(norm.reason, WHY);
  assert.ok(!norm.reason.includes('\u2026'), 'why line must not be truncated');
  const card = logic.captureGuardCard({
    ok: true,
    decision: { action: 'BLOCK', reason: WHY, combination: COMBINATION },
    text: 'x',
  });
  assert.equal(card.body, WHY);
  assert.ok(!card.body.includes('\u2026'), 'card body must not be truncated');
});
