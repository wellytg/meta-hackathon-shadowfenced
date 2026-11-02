'use strict';

/* Popup: three-state status (connected / not running / not paired),
 * pairing-token entry with Paste + Save, per-site on/off switches, and a
 * Test button that checks one sample sentence through the local app.
 * All persisted via the background worker. */

var SAMPLE_TEXT = 'My email is ada@example.com, call me at 555-0100.';

function bg(msg) {
  return new Promise(function (resolve) {
    chrome.runtime.sendMessage(msg, function (res) {
      if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
      else resolve(res || { ok: false });
    });
  });
}

function setStatus(el, icon, text, tone) {
  el.textContent = text;
  icon.className = 'sf-dot sf-' + tone;
}

/* Three-state status from the SF_HEALTH reply (canonical version in
 * logic.js, loaded just before this file; the duplicate below keeps
 * the popup working standalone). {ok:true} is Connected, HTTP 401/403
 * is Not paired, and a network error is Not running. */
function healthStatus(res) {
  var logic = (typeof globalThis !== 'undefined' && globalThis.ShadowfencedLogic) || null;
  if (logic && logic.healthToStatus) return logic.healthToStatus(res);
  /* Tiny duplicate of healthToStatus in logic.js (see src/logic.js). */
  if (res && res.ok) return { text: 'Connected \u00b7 checking locally', tone: 'ok' };
  if (res && (res.status === 401 || res.status === 403)) {
    return { text: 'Not paired \u2014 paste your token', tone: 'warn' };
  }
  return { text: 'Not running \u2014 start Shadowfenced', tone: 'bad' };
}

function siteTone(entry, nowMs) {
  var logic = (typeof globalThis !== 'undefined' && globalThis.ShadowfencedLogic) || null;
  if (logic && logic.siteRowTone) return logic.siteRowTone(entry, nowMs);
  /* Tiny duplicate of siteRowTone in logic.js (see src/logic.js). */
  if (!entry || entry.protecting !== false) return '';
  var now = Number(nowMs) || Date.now();
  if (typeof entry.ts === 'number' && (now - entry.ts) < 120000) return 'amber';
  return '';
}

function probe(statusEl, iconEl) {
  return bg({ type: 'SF_HEALTH' }).then(function (res) {
    var st = healthStatus(res);
    setStatus(statusEl, iconEl, st.text, st.tone);
    return res;
  });
}

function paintSiteRows(prot) {
  var now = Date.now();
  document.querySelectorAll('#sites input[data-site]').forEach(function (box) {
    var id = box.getAttribute('data-site');
    var label = (box.closest && box.closest('label')) || box.parentNode;
    if (!label) return;
    if (siteTone(prot && prot[id], now) === 'amber') {
      label.classList.add('sf-amber');
      label.title = 'Shadowfenced: not protecting this page';
    } else {
      label.classList.remove('sf-amber');
      label.title = '';
    }
  });
}

document.addEventListener('DOMContentLoaded', function () {
  var statusEl = document.getElementById('status');
  var iconEl = document.getElementById('statusIcon');
  var tokenEl = document.getElementById('token');
  var pasteEl = document.getElementById('paste');
  var saveEl = document.getElementById('save');
  var savedEl = document.getElementById('saved');
  var testEl = document.getElementById('test');
  var testResultEl = document.getElementById('testResult');

  chrome.storage.local.get(['sfToken', 'sfSites', 'sfProt'], function (items) {
    var token = items.sfToken || '';
    if (token) tokenEl.value = token;
    probe(statusEl, iconEl);
    var sites = items.sfSites || {};
    document.querySelectorAll('#sites input[data-site]').forEach(function (box) {
      var id = box.getAttribute('data-site');
      box.checked = sites[id] !== false;
      box.addEventListener('change', function () {
        bg({ type: 'SF_SET_SITE', site: id, on: box.checked });
      });
    });
    paintSiteRows(items.sfProt);
  });

  pasteEl.addEventListener('click', function () {
    if (navigator.clipboard && navigator.clipboard.readText) {
      navigator.clipboard.readText().then(function (text) {
        if (text) tokenEl.value = text.trim();
        tokenEl.focus();
      }).catch(function () {
        tokenEl.focus();
      });
    } else {
      tokenEl.focus();
    }
  });

  saveEl.addEventListener('click', function () {
    bg({ type: 'SF_SET_TOKEN', token: tokenEl.value.trim() }).then(function (res) {
      savedEl.hidden = !(res && res.ok);
      if (res && res.ok) {
        setTimeout(function () { savedEl.hidden = true; }, 2000);
        probe(statusEl, iconEl);
      }
    });
  });

  testEl.addEventListener('click', function () {
    testEl.disabled = true;
    testResultEl.hidden = false;
    testResultEl.textContent = 'Checking sample text\u2026';
    testResultEl.className = 'sf-result sf-unknown';
    bg({ type: 'SF_SCAN', text: SAMPLE_TEXT, host: '' }).then(function (res) {
      testEl.disabled = false;
      if (!res || !res.ok) {
        testResultEl.textContent = 'Test failed: ' + ((res && res.error) || 'local app is not running');
        testResultEl.className = 'sf-result sf-bad';
        return;
      }
      var spans = (res.data && res.data.spans) || [];
      if (!spans.length) {
        testResultEl.textContent = 'Test: clean \u2014 the app found nothing in the sample.';
        testResultEl.className = 'sf-result sf-ok';
        return;
      }
      var labels = spans.map(function (s) { return s.label || 'sensitive detail'; });
      var shown = labels.slice(0, 3).join(', ');
      testResultEl.textContent = 'Test: found ' + spans.length + ' detail' +
        (spans.length === 1 ? '' : 's') + ' (' + shown +
        (labels.length > 3 ? ', \u2026' : '') + ').';
      testResultEl.className = 'sf-result sf-warn';
    });
  });
});
