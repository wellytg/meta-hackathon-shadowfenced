'use strict';

/* Shadowfenced shared logic.
 * Pure functions only — no browser APIs — so this file loads in Node
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
    case 'BLOCK': return { action: 'BLOCK', reason: d.reason || 'Blocked by policy.' };
    case 'REDACT': return { action: 'REDACT', safe_text: d.safe_text || '', count: d.count || 0 };
    case 'HOLD':
    case 'WARN': return { action: 'HOLD', reason: d.reason || 'Please review before sending.' };
    case 'ALLOW':
    case 'OK':
    case 'NONE': return { action: 'ALLOW' };
    default: return { action: 'BLOCK', reason: FAILSAFE_MESSAGE };
  }
}

var ShadowfencedLogic = {
  FAILSAFE_MESSAGE: FAILSAFE_MESSAGE,
  selectAdapter: selectAdapter,
  spansToRanges: spansToRanges,
  failSafeDecision: failSafeDecision
};

if (typeof globalThis !== 'undefined') globalThis.ShadowfencedLogic = ShadowfencedLogic;
if (typeof module !== 'undefined' && module.exports) {
  module.exports = ShadowfencedLogic;
}
