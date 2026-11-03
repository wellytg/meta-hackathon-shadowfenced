'use strict';

/* Shadowfenced background worker (service worker on Chrome, background
 * script on Firefox â€” same file, no modules, only chrome.* + fetch).
 * - Stores the pairing token and per-site on/off toggles (chrome.storage).
 * - Forwards scan/redact/rehydrate/health calls to the local app at
 *   http://127.0.0.1:8765 with the X-Ext-Token header.
 * - Bridges messages between content scripts and the local app. */

var SF_BASE = 'http://127.0.0.1:8765';
var SF_SITES = ['chatgpt', 'claude', 'gemini', 'meta'];

function getState() {
  return new Promise(function (resolve) {
    try {
      chrome.storage.local.get(['sfToken', 'sfSites'], function (items) {
        if (chrome.runtime.lastError) {
          resolve({ token: '', sites: {} });
          return;
        }
        resolve({ token: items.sfToken || '', sites: items.sfSites || {} });
      });
    } catch (e) {
      resolve({ token: '', sites: {} });
    }
  });
}

function siteDisabled(sites, host) {
  var id = hostToSiteId(host);
  if (!id) return false;
  return sites[id] === false;
}

function hostToSiteId(host) {
  var h = String(host || '').toLowerCase();
  if (h.indexOf('chatgpt.com') !== -1) return 'chatgpt';
  if (h.indexOf('claude.ai') !== -1) return 'claude';
  if (h.indexOf('gemini.google.com') !== -1) return 'gemini';
  if (h.indexOf('meta.ai') !== -1) return 'meta';
  return null;
}

function callApp(path, token, body) {
  var headers = { 'Content-Type': 'application/json' };
  if (token) headers['X-Ext-Token'] = token;
  var opts = { method: body === undefined ? 'GET' : 'POST', headers: headers };
  if (body !== undefined) opts.body = JSON.stringify(body);
  return fetch(SF_BASE + path, opts).then(function (resp) {
    if (!resp.ok) {
      // Keep the HTTP status: 401/403 means "not paired", anything
      // else (or a network failure with no status) means "not running".
      var err = new Error('app returned HTTP ' + resp.status);
      err.status = resp.status;
      throw err;
    }
    return resp.json();
  });
}

chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  (async function () {
    var state = await getState();
    switch (msg && msg.type) {
      case 'SF_SCAN':
        if (siteDisabled(state.sites, msg.host)) { sendResponse({ ok: true, data: { spans: [] } }); return; }
        sendResponse(await forward('/api/ext/scan', state.token, { text: msg.text || '' }));
        return;
      case 'SF_REDACT':
        // Redact is fail-safe: never short-circuit on toggles here; the
        // content script already skips when the site is off. A disabled
        // lookup failure still resolves {ok:false} -> send is blocked.
        sendResponse(await forward('/api/ext/redact', state.token, { text: msg.text || '' }));
        return;
      case 'SF_REHYDRATE':
        sendResponse(await forward('/api/ext/rehydrate', state.token, { text: msg.text || '' }));
        return;
      case 'SF_CHOICE':
        sendResponse(await forward('/api/ext/choice', state.token, {
          decision_id: msg.decision_id || '',
          choice: msg.choice || '',
          kinds: msg.kinds || [],
          rule_ids: msg.rule_ids || [],
          site: msg.site || '',
          source: msg.source || 'text'
        }));
        return;
      case 'SF_SCAN_FILE':
        sendResponse(await forwardFile('/api/ext/scan-file', state.token, msg));
        return;
      case 'SF_REDACT_FILE':
        sendResponse(await forward('/api/ext/redact-file', state.token, { scan_id: msg.scan_id || '' }));
        return;
      case 'SF_DEEP_START':
        sendResponse(await forward('/api/ext/deep-check', state.token, { scan_id: msg.scan_id || '' }));
        return;
      case 'SF_DEEP_STATUS':
        sendResponse(await forward('/api/ext/deep-check/' + encodeURIComponent(msg.check_id || ''), state.token, undefined));
        return;
      case 'SF_HEALTH':
        sendResponse(await forward('/api/ext/health', state.token, undefined));
        return;
      case 'SF_GET_STATE':
        sendResponse({ ok: true, data: { disabled: siteDisabled(state.sites, msg.host), sites: state.sites } });
        return;
      case 'SF_PROT':
        // Fail-visible report from the content script: whether the
        // composer is currently attached on `msg.site`. The popup reads
        // this (with its timestamp) to paint an amber site row.
        chrome.storage.local.get(['sfProt'], function (items) {
          var map = (items && items.sfProt) || {};
          map[msg.site] = { protecting: !!msg.protecting, ts: Date.now() };
          chrome.storage.local.set({ sfProt: map }, function () {
            sendResponse({ ok: !chrome.runtime.lastError });
          });
        });
        return;
      case 'SF_SET_TOKEN':
        chrome.storage.local.set({ sfToken: msg.token || '' }, function () {
          sendResponse({ ok: !chrome.runtime.lastError });
        });
        return;
      case 'SF_SET_SITE':
        var sites = Object.assign({}, state.sites);
        sites[msg.site] = !!msg.on;
        chrome.storage.local.set({ sfSites: sites }, function () {
          sendResponse({ ok: !chrome.runtime.lastError });
        });
        return;
      default:
        sendResponse({ ok: false, error: 'unknown message' });
    }
  })();
  return true; // async response
});

function b64ToBytes(b64) {
  var bin = atob(String(b64 || ''));
  var bytes = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function forwardFile(path, token, msg) {
  var headers = {};
  if (token) headers['X-Ext-Token'] = token;
  var bytes = b64ToBytes(msg.dataBase64 || '');
  var form = new FormData();
  form.append('file', new Blob([bytes], { type: msg.mime || 'application/octet-stream' }), msg.name || 'upload');
  return fetch(SF_BASE + path, { method: 'POST', headers: headers, body: form }).then(function (resp) {
    if (!resp.ok) {
      var err = new Error('app returned HTTP ' + resp.status);
      err.status = resp.status;
      throw err;
    }
    return resp.json();
  }).then(function (data) {
    return { ok: true, data: data };
  }).catch(function (e) {
    var out = { ok: false, error: String((e && e.message) || e) };
    if (e && typeof e.status === 'number') out.status = e.status;
    return out;
  });
}

function forward(path, token, body) {
  return callApp(path, token, body).then(function (data) {
    return { ok: true, data: data };
  }).catch(function (e) {
    var out = { ok: false, error: String((e && e.message) || e) };
    if (e && typeof e.status === 'number') out.status = e.status;
    return out;
  });
}
