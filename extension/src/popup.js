'use strict';

/* Popup: health probe (connected / not running), pairing-token entry,
 * per-site on/off toggles. All persisted via the background worker. */

function bg(msg) {
  return new Promise(function (resolve) {
    chrome.runtime.sendMessage(msg, function (res) {
      if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
      else resolve(res || { ok: false });
    });
  });
}

document.addEventListener('DOMContentLoaded', function () {
  var statusEl = document.getElementById('status');
  var tokenEl = document.getElementById('token');
  var saveEl = document.getElementById('save');
  var savedEl = document.getElementById('saved');

  bg({ type: 'SF_HEALTH' }).then(function (res) {
    if (res && res.ok) {
      statusEl.textContent = 'Connected to Shadowfenced.';
      statusEl.className = 'sf-status sf-ok';
    } else {
      statusEl.textContent = "Shadowfenced isn't running \u2014 start it or turn the extension off.";
      statusEl.className = 'sf-status sf-bad';
    }
  });

  chrome.storage.local.get(['sfToken', 'sfSites'], function (items) {
    if (items.sfToken) tokenEl.value = items.sfToken;
    var sites = items.sfSites || {};
    document.querySelectorAll('#sites input[data-site]').forEach(function (box) {
      var id = box.getAttribute('data-site');
      box.checked = sites[id] !== false;
      box.addEventListener('change', function () {
        bg({ type: 'SF_SET_SITE', site: id, on: box.checked });
      });
    });
  });

  saveEl.addEventListener('click', function () {
    bg({ type: 'SF_SET_TOKEN', token: tokenEl.value.trim() }).then(function (res) {
      savedEl.hidden = !(res && res.ok);
      if (res && res.ok) {
        setTimeout(function () { savedEl.hidden = true; }, 2000);
        bg({ type: 'SF_HEALTH' }).then(function (h) {
          if (h && h.ok) {
            statusEl.textContent = 'Connected to Shadowfenced.';
            statusEl.className = 'sf-status sf-ok';
          }
        });
      }
    });
  });
});
