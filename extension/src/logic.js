'use strict';

/* Shadowfenced shared logic.
 * Pure functions only â€” no browser APIs â€” so this file loads in Node
 * (tests) as well as in content scripts / service workers.
 * content.js and adapters.js reuse these via globalThis.ShadowfencedLogic
 * (logic.js is listed first in content_scripts); where a direct reference
 * is impractical they carry a tiny commented duplicate instead.
 */

var FAILSAFE_MESSAGE =
  "Shadowfenced isn't running \u2014 start it or turn the extension off";

/* Pick the adapter whose hosts list matches `host`.
 * `adapters` may be an Array of {id, hosts:[...], ...} or a dict of them.
 * Matches exact hosts and subdomains (e.g. "www.meta.ai" matches
 * "meta.ai"). Returns the adapter object or null. */
function selectAdapter(host, adapters) {
  if (!host || !adapters) return null;
  var h = String(host).toLowerCase().split(':')[0];
  var list = Array.isArray(adapters) ? adapters : Object.values(adapters);
  for (var i = 0; i < list.length; i++) {
    var a = list[i];
    var hosts = (a && a.hosts) || [];
    for (var j = 0; j < hosts.length; j++) {
      var want = String(hosts[j]).toLowerCase();
      if (h === want || h.endsWith('.' + want)) return a;
    }
  }
  return null;
}

/* Clamp raw scan spans to [0, textLength], drop empties, sort, and merge
 * overlapping or adjacent ranges. Returns [{start, end}]. */
function spansToRanges(spans, textLength) {
  var n = Math.max(0, Number(textLength) || 0);
  if (!Array.isArray(spans) || n === 0) return [];
  var rs = [];
  for (var i = 0; i < spans.length; i++) {
    var s = spans[i] || {};
    var start = Math.max(0, Math.min(n, Math.floor(Number(s.start) || 0)));
    var end = Math.max(0, Math.min(n, Math.ceil(Number(s.end) || 0)));
    if (end > start) rs.push({ start: start, end: end });
  }
  rs.sort(function (a, b) { return a.start - b.start || a.end - b.end; });
  var out = [];
  for (var k = 0; k < rs.length; k++) {
    var last = out[out.length - 1];
    if (last && rs[k].start <= last.end) {
      if (rs[k].end > last.end) last.end = rs[k].end;
    } else {
      out.push({ start: rs[k].start, end: rs[k].end });
    }
  }
  return out;
}

/* Fail-safe send decision.
 * Input: {ok, decision} where ok=false means the app call failed or the
 * app is not running, and decision is the /api/ext/redact payload
 * ({action: BLOCK|REDACT|HOLD|WARN|ALLOW, ...}).
 * Any failure or unknown action resolves to {action:'BLOCK'} so a send
 * never slips through unchecked. */
function failSafeDecision(input) {
  var args = input || {};
  if (!args.ok) return { action: 'BLOCK', reason: FAILSAFE_MESSAGE };
  var d = args.decision || {};
  switch (d.action) {
    case 'BLOCK': {
      var blk = { action: 'BLOCK', reason: d.reason || 'Blocked by policy.' };
      if (d && Array.isArray(d.combination)) blk.combination = d.combination.slice(0, 12);
      if (d && typeof d.generalised_text === 'string' && d.generalised_text) blk.generalised_text = d.generalised_text;
      return blk;
    }
    case 'REDACT': return { action: 'REDACT', safe_text: d.safe_text || '', count: d.count || 0 };
    case 'HOLD':
    case 'WARN': return { action: 'HOLD', reason: d.reason || 'Please review before sending.' };
    case 'ALLOW':
    case 'OK':
    case 'NONE': return { action: 'ALLOW' };
    default: return { action: 'BLOCK', reason: FAILSAFE_MESSAGE };
  }
}

/* Popup three-state status from a background SF_HEALTH reply.
 * {ok:true} means Connected; HTTP 401/403 (missing or wrong token, bad
 * origin) means Not paired; anything else (network error, app down)
 * means Not running. */
function healthToStatus(res) {
  if (res && res.ok) return { text: 'Connected \u00b7 checking locally', tone: 'ok' };
  if (res && (res.status === 401 || res.status === 403)) {
    return { text: 'Not paired \u2014 paste your token', tone: 'warn' };
  }
  return { text: 'Not running \u2014 start Shadowfenced', tone: 'bad' };
}

/* Map the /api/ext/redact payload ({decision, safe_text, notes, id})
 * onto the internal send-decision shape ({action, reason, safe_text,
 * count}) that failSafeDecision consumes. Unknown shapes map to {} so
 * the fail-safe default (BLOCK) applies downstream. */
function countPlaceholders(text) {
  var m = String(text || '').match(/\[[A-Z][A-Z0-9_]*\]/g);
  return m ? m.length : 0;
}
function normalizeRedact(data) {
  var d = data || {};
  var action = String(d.decision || d.action || '').toUpperCase();
  if (action === 'BLOCK') {
    var blocked = { action: 'BLOCK', reason: d.notes || d.reason || 'Blocked by policy.' };
    if (Array.isArray(d.combination)) blocked.combination = d.combination.slice(0, 12);
    if (typeof d.generalised_text === 'string' && d.generalised_text) blocked.generalised_text = d.generalised_text;
    return blocked;
  }
  if (action === 'REDACT') {
    return { action: 'REDACT', safe_text: d.safe_text || '', count: countPlaceholders(d.safe_text) };
  }
  if (action === 'HOLD' || action === 'WARN') {
    return { action: 'HOLD', reason: d.notes || d.reason || 'Please review before sending.' };
  }
  if (action === 'ALLOW' || action === 'OK' || action === 'NONE') return { action: 'ALLOW' };
  return {};
}

/* Local secret heuristic for the capture guard's app-down path: when
 * the app is unreachable nothing can be scanned, so text that already
 * looks secret-bearing never gets a "Send anyway" override. */
function looksLikeSecret(text) {
  var s = String(text || '');
  if (/AKIA[0-9A-Z]{16}/.test(s)) return true;
  if (/xox[bpas]-[A-Za-z0-9-]{10,}/.test(s)) return true;
  if (/ghp_[A-Za-z0-9]{20,}/.test(s)) return true;
  if (/sk-[A-Za-z0-9]{20,}/.test(s)) return true;
  if (/-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/.test(s)) return true;
  return false;
}

var NOT_PROTECTING_TEXT = 'Shadowfenced: not protecting this page';
var NOT_RUNNING_HELD_TEXT = 'Shadowfenced is not running \u2014 message not sent';

/* Whether the document-level capture guard should step in for this
 * event. `o` carries {kind:'enter'|'click', key, shift, composing,
 * supported, adapterAttached, armed, sending, siteOn, activeEditable,
 * onSendButton}. */
function guardShouldIntercept(o) {
  o = o || {};
  if (!o.supported || o.adapterAttached || o.armed || o.sending || !o.siteOn) return false;
  if (o.kind === 'enter') {
    if (o.key !== 'Enter' || o.shift || o.composing) return false;
    return !!o.activeEditable;
  }
  if (o.kind === 'click') return !!(o.onSendButton && o.activeEditable);
  return false;
}

/* Card the capture guard shows for a held send. {send:true} lets the
 * send proceed; otherwise {send:false, tone, title, body, buttons}
 * with button ids drawn from 'send-anyway' | 'ok' | 'cancel'. */
function captureGuardCard(input) {
  var args = input || {};
  if (args.timeout) return sendFailureCard({ timeout: true });
  if (args.status === 401 || args.status === 403) {
    return sendFailureCard({ status: args.status });
  }
  if (!args.ok) {
    if (looksLikeSecret(args.text || '')) {
      return {
        send: false, tone: 'block', title: "Shadowfenced isn't running",
        body: NOT_RUNNING_HELD_TEXT + ' This message looks like it carries a secret, so it stays held until the app is back.',
        buttons: ['ok']
      };
    }
    return {
      send: false, tone: 'block', title: "Shadowfenced isn't running",
      body: NOT_RUNNING_HELD_TEXT + ' Nothing was sent.',
      buttons: ['send-anyway', 'cancel']
    };
  }
  var d = args.decision || {};
  if (d.action === 'BLOCK') {
    return {
      send: false, tone: 'block', title: 'Blocked \u2014 not sent',
      body: d.reason || 'Blocked by policy.', buttons: ['ok']
    };
  }
  return { send: true };
}

/* Attachment counting: real attachment chips and file inputs that
 * actually hold files count; buttons (Upload & tools, Add attachment)
 * and empty or non-file inputs never do. `el` needs only
 * tagName/type/files, so this stays testable without a browser. */
function countsAsAttachment(el) {
  if (!el) return false;
  var tag = String(el.tagName || '').toUpperCase();
  if (tag === 'INPUT') {
    if (String(el.type || '').toLowerCase() !== 'file') return false;
    return !!(el.files && el.files.length);
  }
  if (tag === 'BUTTON') return false;
  return true;
}

/* The hover card must never steal keyboard focus from the composer;
 * notice cards still take focus so Escape and buttons work at once. */
function shouldFocusCard(mode) {
  return mode !== 'hover';
}

/* Any editable element the capture guard should watch, whether or not
 * an adapter attached to it (textarea, text-like input,
 * contenteditable, role=textbox). */
function isEditableElement(el) {
  if (!el) return false;
  var tag = String(el.tagName || '').toUpperCase();
  if (tag === 'TEXTAREA') return true;
  if (tag === 'INPUT') {
    var t = String(el.type || 'text').toLowerCase();
    return (t !== 'file' && t !== 'hidden' && t !== 'checkbox' && t !== 'radio' &&
      t !== 'button' && t !== 'submit' && t !== 'image' && t !== 'reset');
  }
  try {
    if (el.isContentEditable) return true;
  } catch (e) { /* ignore */ }
  if (el.getAttribute) {
    var ce = null;
    try { ce = el.getAttribute('contenteditable'); } catch (e2) { ce = null; }
    if (ce !== null && ce !== undefined && String(ce).toLowerCase() !== 'false') return true;
    var role = null;
    try { role = el.getAttribute('role'); } catch (e3) { role = null; }
    if (role === 'textbox') return true;
  }
  return false;
}

/* Fail-visible state on a supported host: protecting while the
 * composer is attached, unprotected (badge plus amber site row) when
 * no composer is found. */
function protectionStatus(opts) {
  var o = opts || {};
  if (!o.supported) return 'unsupported';
  return o.editorFound ? 'protecting' : 'unprotected';
}

/* Popup site-row tone from the last content-script protection report
 * {protecting, ts}; reports older than PROT_STALE_MS read as unknown. */
var PROT_STALE_MS = 120000;
function siteRowTone(entry, nowMs) {
  if (!entry || entry.protecting !== false) return '';
  var now = Number(nowMs);
  if (!now) {
    try { now = Date.now(); } catch (e) { return ''; }
  }
  if (typeof entry.ts === 'number' && (now - entry.ts) < PROT_STALE_MS) return 'amber';
  return '';
}

/* Card for a send check that failed without a verdict. `input`
 * carries {status, timeout}: a check that never answered (timeout)
 * offers Retry; HTTP 401/403 means the app is running but the
 * extension is not paired; anything else is the app not running. */
function sendFailureCard(input) {
  var args = input || {};
  if (args.timeout) {
    return {
      send: false, tone: 'review', title: 'Still no answer',
      body: 'Still no answer \u2014 nothing was sent.',
      buttons: ['retry', 'cancel']
    };
  }
  if (args.status === 401 || args.status === 403) {
    return {
      send: false, tone: 'block', title: 'Not paired',
      body: 'Not paired \u2014 open the Shadowfenced popup and paste the pairing token. Nothing was sent.',
      buttons: ['ok']
    };
  }
  return {
    send: false, tone: 'block', title: "Shadowfenced isn't running",
    body: FAILSAFE_MESSAGE + ' Nothing was sent.',
    buttons: ['ok']
  };
}

var ShadowfencedLogic = {
  FAILSAFE_MESSAGE: FAILSAFE_MESSAGE,
  NOT_PROTECTING_TEXT: NOT_PROTECTING_TEXT,
  NOT_RUNNING_HELD_TEXT: NOT_RUNNING_HELD_TEXT,
  PROT_STALE_MS: PROT_STALE_MS,
  selectAdapter: selectAdapter,
  spansToRanges: spansToRanges,
  failSafeDecision: failSafeDecision,
  healthToStatus: healthToStatus,
  normalizeRedact: normalizeRedact,
  countPlaceholders: countPlaceholders,
  looksLikeSecret: looksLikeSecret,
  guardShouldIntercept: guardShouldIntercept,
  captureGuardCard: captureGuardCard,
  sendFailureCard: sendFailureCard,
  countsAsAttachment: countsAsAttachment,
  shouldFocusCard: shouldFocusCard,
  isEditableElement: isEditableElement,
  protectionStatus: protectionStatus,
  siteRowTone: siteRowTone,
  buildChoicePayload: buildChoicePayload,
  CHOICE_NAMES: CHOICE_NAMES
};

/* Fields-only choice payload for POST /api/ext/choice (T69).
 * Input: {decision_id, choice, kinds, rule_ids, site, source}.
 * Returns the cleaned payload, or null when the decision id or the
 * choice is unusable. Kinds keep the strict cls:subclass shape (other
 * entries are dropped); the site is reduced to its host only. Never
 * carries values, message text, or file names. */
var CHOICE_NAMES = [
  'send_anyway', 'send_generalised', 'generalise', 'cancel',
  'redacted_copy', 'upload_anyway', 'approve', 'deny',
  'apply_suggestion', 'dismiss_suggestion'
];
var CHOICE_KIND_CLASSES = { secret: 1, pii: 1, business: 1, beacon: 1 };
ShadowfencedLogic.CHOICE_NAMES = CHOICE_NAMES;
function choiceSiteOnly(value) {
  var text = String(value || '').trim().toLowerCase();
  if (text.indexOf('://') !== -1) text = text.split('://')[1];
  text = text.split('/')[0].split('?')[0].split('#')[0];
  if (text.charAt(0) === '[' && text.indexOf(']') !== -1) {
    text = text.slice(1).split(']')[0];
  } else {
    text = text.split(':')[0];
  }
  text = text.trim().replace(/\.+$/, '');
  if (!text) return 'app';
  if (text === 'app' || text === 'localhost') return text;
  if (!/^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$/.test(text)) return 'app';
  return text;
}
function buildChoicePayload(input) {
  var args = input || {};
  var decisionId = String(
    args.decisionId !== undefined ? args.decisionId : args.decision_id || ''
  ).trim();
  if (!/^[A-Za-z0-9_.-]{1,128}$/.test(decisionId)) return null;
  var choice = String(args.choice || '').trim().toLowerCase();
  if (CHOICE_NAMES.indexOf(choice) === -1) return null;
  var kinds = [];
  var rawKinds = Array.isArray(args.kinds) ? args.kinds : [];
  for (var i = 0; i < rawKinds.length && kinds.length < 32; i++) {
    var label = String(rawKinds[i] || '').trim().toLowerCase();
    var parts = label.split(':');
    if (parts.length !== 2) continue;
    if (!/^[a-z0-9_]{1,32}$/.test(parts[0])) continue;
    if (!/^[a-z0-9_]{1,48}$/.test(parts[1])) continue;
    if (!CHOICE_KIND_CLASSES[parts[0]]) continue;
    if (kinds.indexOf(label) === -1) kinds.push(label);
  }
  var ruleIds = [];
  var rawRules = Array.isArray(args.rule_ids) ? args.rule_ids : [];
  for (var j = 0; j < rawRules.length && ruleIds.length < 32; j++) {
    var rule = String(rawRules[j] || '').trim();
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(rule)) continue;
    if (ruleIds.indexOf(rule) === -1) ruleIds.push(rule);
  }
  var source = String(args.source || 'text').trim().toLowerCase();
  return {
    decision_id: decisionId,
    choice: choice,
    kinds: kinds,
    rule_ids: ruleIds,
    site: choiceSiteOnly(args.site),
    source: source === 'file' ? 'file' : 'text'
  };
}

if (typeof globalThis !== 'undefined') globalThis.ShadowfencedLogic = ShadowfencedLogic;
if (typeof module !== 'undefined' && module.exports) {
  module.exports = ShadowfencedLogic;
}
