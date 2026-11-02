"""T24: descriptive-mode trace display + splash page width and contrast.

- ``/api/send`` always carries a decision ``trace`` for ALLOW, REDACT,
  BLOCK and HOLD (the UI renders one trace per send response).
- ``rules.js`` pairs each send-response trace with its banner: a queue
  entry is created synchronously per ``/api/send`` fetch (so a banner
  appended before the trace JSON resolves still gets its trace), the
  ``MutationObserver`` watches for banners, the trace renders right
  after its banner with ``data-testid="sf-trace"``, and turning the
  toggle off hides traces.
- ``welcome.css`` scales at 400px viewport width and gives the
  "Three things to know" label high-contrast colors.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from shadowfenced import app as app_module
from shadowfenced.app import SendRequest, send
from shadowfenced.canary import CanaryRegistry

SEED = "test-seed-t24"
KINDS = ("db_password", "api_key")
ENTROPY_TOKEN = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"
RULES_JS = STATIC / "rules.js"
RULES_CSS = STATIC / "rules.css"
WELCOME_CSS = STATIC / "welcome" / "welcome.css"
WELCOME_HTML = STATIC / "welcome" / "index.html"
APP_JS = STATIC / "app.js"


@pytest.fixture()
def mock_upstream():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    app_module.app.state.upstream_transport = httpx.MockTransport(handler)
    yield
    if hasattr(app_module.app.state, "upstream_transport"):
        delattr(app_module.app.state, "upstream_transport")


def _canary() -> str:
    return CanaryRegistry(SEED, KINDS).value("db_password")


def _assert_trace_shape(body: dict, decision: str) -> None:
    assert body["decision"] == decision
    trace = body["trace"]
    assert isinstance(trace, list) and trace, "send must carry a trace"
    for step in trace:
        assert set(step) == {"step", "tool", "detail", "result", "ms"}
    assert trace[-1]["step"] == "decision"
    assert trace[-1]["result"] == decision


# --- /api/send carries a trace for every decision ---------------------------


def test_send_trace_allow(mock_upstream):
    body = send(SendRequest(messages=[{"role": "user", "content": "hello there"}]))
    _assert_trace_shape(body, "ALLOW")


def test_send_trace_redact(mock_upstream):
    secret = "ada@corvanelabs.com"
    body = send(
        SendRequest(messages=[{"role": "user", "content": f"mail {secret} now"}])
    )
    _assert_trace_shape(body, "REDACT")
    assert secret not in json.dumps(body["trace"])


def test_send_trace_block(mock_upstream):
    canary = _canary()
    body = send(
        SendRequest(messages=[{"role": "user", "content": f"password: {canary}"}])
    )
    _assert_trace_shape(body, "BLOCK")
    assert canary not in json.dumps(body["trace"])


def test_send_trace_hold(mock_upstream):
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": f"maybe {ENTROPY_TOKEN}"}]
        )
    )
    _assert_trace_shape(body, "HOLD")


# --- rules.js pairs one trace per send response ------------------------------


def _rules() -> str:
    return RULES_JS.read_text(encoding="utf-8")


def test_rules_js_queues_entry_synchronously_per_send():
    js = _rules()
    # One queue entry per fetch, pushed synchronously (not inside the async
    # clone().json() callback), so the banner can never win the race.
    assert "pendingSends.push(tracePromise)" in js
    assert "pendingTraces.push" not in js


def test_rules_js_pairs_banners_with_traces():
    js = _rules()
    assert "MutationObserver" in js
    assert 'contains("banner")' in js
    assert "pairTraces" in js
    assert 'insertAdjacentElement("afterend"' in js
    assert 'data-testid", "sf-trace"' in js


def test_rules_js_toggle_hides_traces():
    js = _rules()
    assert "setTracesVisible" in js
    assert 'querySelectorAll(".sf-trace")' in js
    assert "sf-descriptive-toggle" in js


def test_rules_js_keeps_hooks_and_ids():
    js = _rules()
    found = set(re.findall(r'data-testid",\s*"([^"]+)"', js))
    assert found == {
        "sf-trace",
        "sf-drawer",
        "sf-custom-rules",
        "sf-value",
        "sf-add",
        "sf-error",
        "sf-rules-btn",
        "sf-descriptive-toggle",
        "sf-catalog",
        "sf-catalog-entry",
        "sf-catalog-why",
        "sf-catalog-footer",
    }
    assert set(re.findall(r'getElementById\("([^"]+)"\)', js)) == {"thread"}


def test_rules_js_renders_any_decision_result():
    js = _rules()
    # One generic renderer covers BLOCK, REDACT, ALLOW and HOLD step results.
    assert 'sf-result-" + String(entry.result' in js


def test_rules_css_hides_traces():
    css = RULES_CSS.read_text(encoding="utf-8")
    assert ".sf-trace[hidden]" in css


def test_app_js_knows_nothing_about_traces():
    js = APP_JS.read_text(encoding="utf-8")
    assert "sf-trace" not in js
    assert "pendingSends" not in js


NODE_PROBE = r"""
"use strict";
const fs = require("fs");
let observers = [];
class El {
  constructor(tag) {
    this.nodeType = 1; this.tagName = tag; this.children = [];
    this.parentNode = null; this._class = ""; this.attributes = {};
    this.hidden = false; this.textContent = ""; this.listeners = {};
  }
  set className(v) { this._class = String(v); }
  get className() { return this._class; }
  get classList() {
    const s = this;
    return { contains(c) { return s._class.split(/\s+/).indexOf(c) >= 0; } };
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  click() { (this.listeners.click || []).forEach((fn) => fn({})); }
  appendChild(c) { c.parentNode = this; this.children.push(c); fire(this, c); return c; }
  insertAdjacentElement(pos, el) {
    const p = this.parentNode; const i = p.children.indexOf(this);
    el.parentNode = p; p.children.splice(i + 1, 0, el); fire(p, el); return el;
  }
  get nextElementSibling() {
    if (!this.parentNode) return null;
    const s = this.parentNode.children;
    return s[s.indexOf(this) + 1] || null;
  }
  get isConnected() { return true; }
  querySelectorAll(sel) {
    const out = []; const want = sel[0] === "." ? sel.slice(1) : null;
    (function walk(n) {
      n.children.forEach((c) => {
        if (want && c._class.split(/\s+/).indexOf(want) >= 0) out.push(c);
        walk(c);
      });
    })(this);
    return out;
  }
}
function fire(parent, added) {
  observers.forEach((o) => {
    let n = parent, inside = false;
    while (n) { if (n === o.target) { inside = true; break; } n = n.parentNode; }
    if (inside) o.cb([{ addedNodes: [added] }]);
  });
}
const thread = new El("div"), bar = new El("header"), bodyEl = new El("body");
bodyEl.appendChild(thread); bodyEl.appendChild(bar);
global.document = {
  createElement: (t) => new El(t),
  getElementById: (id) => (id === "thread" ? thread : null),
  querySelector: (sel) => (sel === ".topbar" ? bar : null),
  querySelectorAll: (sel) => bodyEl.querySelectorAll(sel),
  addEventListener: () => {}, body: bodyEl,
};
global.MutationObserver = class {
  constructor(cb) { this.cb = cb; this.target = null; }
  observe(t) { this.target = t; observers.push(this); }
};
global.window = {
  localStorage: {
    _v: {},
    getItem(k) { return this._v[k] || null; },
    setItem(k, v) { this._v[k] = v; },
  },
  fetch: null,
};
function fakeSendResponse(trace) {
  return {
    clone() {
      return { json() { return new Promise((res) => setTimeout(() => res({ trace }), 20)); } };
    },
    json() { return Promise.resolve({ trace: trace, decision: "REDACT" }); },
  };
}
let pendingResponder = null;
global.window.fetch = function (url) {
  if (typeof url === "string" && url.indexOf("/api/send") >= 0) {
    return new Promise((resolve) => { pendingResponder = resolve; });
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
};
eval(fs.readFileSync("src/shadowfenced/static/rules.js", "utf8"));
const banners = () => thread.children.filter((c) => c._class.split(/\s+/).indexOf("banner") >= 0);
const traces = () => bodyEl.querySelectorAll(".sf-trace");
const toggle = bar.children[bar.children.length - 1].children.filter(
  (c) => c.attributes["data-testid"] === "sf-descriptive-toggle")[0];
function appSend() {
  return global.window.fetch("/api/send", { method: "POST" })
    .then((r) => r.json())
    .then(() => {
      const b = document.createElement("div");
      b.className = "banner";
      b.setAttribute("data-testid", "banner");
      b.textContent = "Sent with 1 redaction.";
      thread.appendChild(b);
      return b;
    });
}
const tick = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
const check = (name, cond) => { results.push([name, !!cond]); };
(async () => {
  toggle.click();
  const trace = [
    { step: "scan", tool: "tier1", detail: "email", result: "REDACT", ms: 1 },
    { step: "decision", tool: "gate", detail: "resolved tier 1", result: "REDACT", ms: 2 },
  ];
  const p = appSend();
  pendingResponder(fakeSendResponse(trace));
  await p;
  check("banner-before-trace", banners().length === 1 && traces().length === 0);
  await tick(60);
  check("trace-rendered", traces().length === 1);
  check("trace-after-banner", banners()[0].nextElementSibling === traces()[0]);
  check("trace-testid", traces()[0].attributes["data-testid"] === "sf-trace");
  check("trace-visible", traces()[0].hidden === false);
  toggle.click();
  check("toggle-off-hides", traces().length === 1 && traces().every((t) => t.hidden === true));
  console.log("PROBE " + JSON.stringify(results));
})().catch((e) => { console.log("PROBE-ERROR " + (e && e.message)); process.exit(1); });
"""


def test_rules_js_renders_trace_for_send_response(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node is not available")
    probe = tmp_path / "t24-probe.js"
    probe.write_text(NODE_PROBE, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(probe)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = next(
        line for line in proc.stdout.splitlines() if line.startswith("PROBE ")
    )
    results = dict(json.loads(line[len("PROBE "):]))
    assert results, "probe reported no checks"
    assert all(results.values()), results


# --- splash page width and contrast ------------------------------------------


def _welcome_css() -> str:
    return WELCOME_CSS.read_text(encoding="utf-8")


def test_welcome_replaced_media_still_present():
    css = _welcome_css()
    assert "@media (max-width: 560px)" in css


def test_welcome_masthead_wraps_and_scales():
    css = _welcome_css()
    assert "img, svg" in css and "max-width: 100%" in css
    masthead = re.search(r"\.masthead\s*\{([^}]*)\}", css).group(1)
    assert "flex-wrap: wrap" in masthead
    assert ".brand > div" in css
    assert ".masthead-nav" in css


def test_welcome_figure_and_try_scale():
    css = _welcome_css()
    figure = re.search(r"\.figure\s*\{([^}]*)\}", css).group(1)
    assert "max-width: 100%" in figure
    assert "overflow: hidden" in figure
    assert ".figure svg" in css
    assert "width: 100%" in css
    assert "flex-wrap: wrap" in css  # .try strip wraps instead of overflowing
    assert ".console-button" in css


def _luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4)]

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_welcome_points_kicker_contrast():
    css = _welcome_css()
    root = re.search(r":root\s*\{([^}]*)\}", css, re.DOTALL).group(1)
    variables = dict(re.findall(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", root))
    block = re.search(r"\.points\s+\.kicker\s*\{([^}]*)\}", css).group(1)

    def color_of(prop: str) -> str:
        value = re.search(rf"{prop}\s*:\s*([^;]+);", block).group(1).strip()
        var = re.fullmatch(r"var\(--([\w-]+)\)", value)
        if var:
            return variables[var.group(1)]
        return value

    fg, bg = color_of("color"), color_of("background")
    lighter, darker = sorted(
        (_luminance(fg), _luminance(bg)), reverse=True
    )
    ratio = (lighter + 0.05) / (darker + 0.05)
    assert ratio >= 7.0, f"kicker contrast {ratio:.2f} for {fg} on {bg}"


def test_welcome_html_hooks_intact():
    html = WELCOME_HTML.read_text(encoding="utf-8")
    assert "<svg" in html
    assert 'name="viewport"' in html
    assert {"open-console", "open-console-main"} <= set(re.findall(r'data-testid="([^"]+)"', html))
    # Only the pre-existing SVG ids; nothing added or renamed.
    assert {"fence-title", "fence-desc", "dots"} <= set(re.findall(r"(?<![\w-])id=\"([^\"]+)\"", html))
