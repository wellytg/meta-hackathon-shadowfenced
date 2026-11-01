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

function probe(statusEl, iconEl, tokenKnown) {
  return bg({ type: 'SF_HEALTH' }).then(function (res) {
    if (res && res.ok) {
      setStatus(statusEl, iconEl, 'Connected \u00b7 checking locally', 'ok');
    } else if (!tokenKnown) {
      setStatus(statusEl, iconEl, 'Not paired \u2014 paste your token', 'warn');
    } else {
      setStatus(statusEl, iconEl, 'Not running \u2014 start Shadowfenced', 'bad');
    }
    return res;
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

  chrome.storage.local.get(['sfToken', 'sfSites'], function (items) {
    var token = items.sfToken || '';
    if (token) tokenEl.value = token;
    probe(statusEl, iconEl, !!token);
    var sites = items.sfSites || {};
    document.querySelectorAll('#sites input[data-site]').forEach(function (box) {
      var id = box.getAttribute('data-site');
      box.checked = sites[id] !== false;
      box.addEventListener('change', function () {
        bg({ type: 'SF_SET_SITE', site: id, on: box.checked });
      });
    });
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
        probe(statusEl, iconEl, !!tokenEl.value.trim());
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
