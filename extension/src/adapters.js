'use strict';

/* Site adapters â€” the five supported chat sites plus the local
 * demo-page adapter in this one file.
 * Each adapter carries CSS selectors for the message editor, the send
 * button, the AI reply area, and file-attachment affordances.
 * Reuses selectAdapter from logic.js (loaded first in content_scripts);
 * the fallback below is a tiny duplicate for non-module contexts.
 * See src/logic.js for the canonical implementation. */

var ADAPTERS = [
  {
    id: 'chatgpt',
    hosts: ['chatgpt.com'],
    editor: [
      '#mobile-composer-prompt',
      'textarea#mobile-composer-prompt',
      'textarea[aria-label*="Chat with ChatGPT" i]',
      'textarea[placeholder*="Ask ChatGPT" i]',
      'form textarea',
      'main textarea',
      '#prompt-textarea',
      'div[contenteditable="true"]#prompt-textarea',
      'form div[contenteditable="true"]',
      'main div[contenteditable="true"]'
    ],
    sendButton: [
      'button[aria-label="Send message"]',
      '[data-testid="send-button"]',
      'form button[data-testid="send-button"]'
    ],
    replyArea: [
      '[data-message-author-role="assistant"]',
      'main [data-testid^="conversation-turn"]'
    ],
    attachment: [
      'input[type="file"]',
      '[data-testid*="attach" i]:not(button)',
      '[data-testid*="file" i]:not(button)'
    ]
  },
  {
    id: 'claude',
    hosts: ['claude.ai'],
    editor: [
      'div[contenteditable="true"]',
      'fieldset div[contenteditable="true"]'
    ],
    sendButton: [
      'button[aria-label="Send message"]',
      'fieldset button[type="submit"]'
    ],
    replyArea: [
      '.font-claude-message',
      '[data-testid="message"]'
    ],
    attachment: [
      'input[type="file"]',
      '[data-testid*="attach" i]:not(button)',
      '[data-testid*="file" i]:not(button)'
    ]
  },
  {
    id: 'gemini',
    hosts: ['gemini.google.com'],
    editor: [
      'div[contenteditable="true"]',
      'rich-textarea div[contenteditable="true"]',
      '.ql-editor[contenteditable="true"]'
    ],
    sendButton: [
      'button[aria-label*="Send" i]',
      '.send-button'
    ],
    replyArea: [
      'message-content',
      '.markdown-main-panel'
    ],
    attachment: [
      'input[type="file"]',
      '[data-testid*="attach" i]:not(button)',
      '[data-testid*="file" i]:not(button)',
      '.attachment-chip',
      '[aria-label*="attached" i]'
    ]
  },
  {
    id: 'meta',
    hosts: ['meta.ai'],
    editor: [
      'input[placeholder*="Ask Meta AI" i]',
      'input[aria-label*="Ask Meta AI" i]',
      'input[placeholder]',
      'div[contenteditable="true"]',
      'textarea',
      '[role="textbox"]'
    ],
    sendButton: [
      'button[aria-label="Send"]',
      'button[aria-label*="Send" i]',
      'form button[type="submit"]'
    ],
    replyArea: [
      '[data-testid="bot-message"]',
      '.ai-response'
    ],
    attachment: [
      'input[type="file"]',
      '[data-testid*="attach" i]:not(button)',
      '[data-testid*="file" i]:not(button)',
      '[aria-label*="attached" i]'
    ]
  },
  {
    id: 'grok',
    hosts: ['grok.com'],
    editor: [
      'textarea[aria-label*="Ask Grok" i]',
      'form textarea',
      'textarea'
    ],
    sendButton: [
      'form button[type="submit"]',
      'button[aria-label*="Submit" i]',
      'button[aria-label*="Send" i]'
    ],
    replyArea: [
      'main [role="log"]',
      'main article',
      'main'
    ],
    attachment: [
      'input[type="file"]'
    ]
  },
  {
    id: 'testpage',
    // Local demo page only. The release manifest never matches loopback,
    // so this adapter is reachable solely through the dev build
    // (build.py --dev), which adds the test-page match pattern.
    hosts: ['127.0.0.1', 'localhost'],
    editor: [
      '#composer-text',
      '#composer-edit',
      '[data-sf-test-composer]'
    ],
    sendButton: [
      '#sendBtn'
    ],
    replyArea: [
      '#replyArea'
    ],
    attachment: [
      '#attachInput',
      'input[type="file"]'
    ]
  }
];

/* Duplicate of selectAdapter in logic.js for contexts where logic.js is
 * not loaded ahead of this file. Prefer the canonical version. */
function _fallbackSelectAdapter(host, adapters) {
  if (!host || !adapters) return null;
  var h = String(host).toLowerCase().split(':')[0];
  var list = Array.isArray(adapters) ? adapters : Object.values(adapters);
  for (var i = 0; i < list.length; i++) {
    var hosts = (list[i] && list[i].hosts) || [];
    for (var j = 0; j < hosts.length; j++) {
      var want = String(hosts[j]).toLowerCase();
      if (h === want || h.endsWith('.' + want)) return list[i];
    }
  }
  return null;
}

function selectAdapter(host) {
  var logic = (typeof globalThis !== 'undefined' && globalThis.ShadowfencedLogic) || null;
  if (logic && logic.selectAdapter) return logic.selectAdapter(host, ADAPTERS);
  return _fallbackSelectAdapter(host, ADAPTERS);
}

function queryFirst(selectors, root) {
  var scope = root || document;
  for (var i = 0; i < selectors.length; i++) {
    var el = null;
    try { el = scope.querySelector(selectors[i]); } catch (e) { el = null; }
    if (el) return el;
  }
  return null;
}

if (typeof globalThis !== 'undefined') {
  globalThis.ShadowfencedAdapters = {
    ADAPTERS: ADAPTERS,
    selectAdapter: selectAdapter,
    queryFirst: queryFirst
  };
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { ADAPTERS: ADAPTERS, selectAdapter: selectAdapter, queryFirst: queryFirst };
}
