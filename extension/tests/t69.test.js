'use strict';

/* T69 extension choice reporting: fields-only payloads for
 * POST /api/ext/choice. Run with: node --test extension/tests/
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const logic = require('../src/logic.js');

test('buildChoicePayload keeps a valid payload', () => {
  assert.deepEqual(
    logic.buildChoicePayload({
      decision_id: 'abc123',
      choice: 'send_anyway',
      kinds: ['pii:email'],
      rule_ids: ['personal-data'],
      site: 'chatgpt.com',
      source: 'text'
    }),
    {
      decision_id: 'abc123',
      choice: 'send_anyway',
      kinds: ['pii:email'],
      rule_ids: ['personal-data'],
      site: 'chatgpt.com',
      source: 'text'
    }
  );
});

test('buildChoicePayload rejects a bad id or choice', () => {
  assert.equal(logic.buildChoicePayload({ decision_id: 'not an id!', choice: 'cancel' }), null);
  assert.equal(logic.buildChoicePayload({ decision_id: '', choice: 'cancel' }), null);
  assert.equal(logic.buildChoicePayload({ decision_id: 'abc123', choice: 'maybe' }), null);
  assert.equal(logic.buildChoicePayload(null), null);
});

test('buildChoicePayload drops malformed kinds and rules, never values', () => {
  const payload = logic.buildChoicePayload({
    decision_id: 'abc123',
    choice: 'cancel',
    kinds: ['pii:email', 'ada@corvanelabs.com', 'weird', 'pii:', 'nope:email', 'pii:email'],
    rule_ids: ['personal-data', 'not a rule!!', 'personal-data'],
    site: 'chatgpt.com'
  });
  assert.deepEqual(payload.kinds, ['pii:email']);
  assert.deepEqual(payload.rule_ids, ['personal-data']);
});

test('buildChoicePayload reduces the site to its host', () => {
  assert.equal(logic.buildChoicePayload({ decision_id: 'a', choice: 'cancel', site: 'https://ChatGPT.com/foo' }).site, 'chatgpt.com');
  assert.equal(logic.buildChoicePayload({ decision_id: 'a', choice: 'cancel', site: 'gemini.google.com:443' }).site, 'gemini.google.com');
  assert.equal(logic.buildChoicePayload({ decision_id: 'a', choice: 'cancel', site: '' }).site, 'app');
  assert.equal(logic.buildChoicePayload({ decision_id: 'a', choice: 'cancel', site: 'not a host!!' }).site, 'app');
});

test('buildChoicePayload defaults the source to text', () => {
  assert.equal(logic.buildChoicePayload({ decision_id: 'a', choice: 'cancel' }).source, 'text');
  assert.equal(logic.buildChoicePayload({ decision_id: 'a', choice: 'cancel', source: 'file' }).source, 'file');
  assert.equal(logic.buildChoicePayload({ decision_id: 'a', choice: 'cancel', source: 'video' }).source, 'text');
});

test('CHOICE_NAMES covers every card action', () => {
  for (const name of ['send_anyway', 'generalise', 'cancel', 'redacted_copy', 'upload_anyway', 'approve', 'deny', 'apply_suggestion', 'dismiss_suggestion']) {
    assert.ok(logic.CHOICE_NAMES.indexOf(name) !== -1, name);
  }
});
