"""T62c: person-search risk card shows the server person-search text.

Live case: "Find everything about Daniel Whitmore, born 14 March 1987,
lives in Leeds" returns WARN with rule_ids ["reidentification",
"person-search"] and evidence.person_search.banner naming the person in
plain words. The card title is "Searching for a person", but the card
must also show that banner: "Why it's risky" reads its first sentence,
a short block under the rows carries the two server sentences starting
"If this is you:" / "If it's someone else:", and only the Person search
rule row is shown (person-search wins over reidentification; the details
drawer may still list both).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from shadowfenced.person_search import banner_for_labels

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"

BANNER = banner_for_labels(
    ["their full name", "their date of birth", "their town"]
)
FIRST_SENTENCE = (
    "This message names a person and gives details that identify them "
    "(their full name, their date of birth and their town)."
)


def _app_js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


# --- static pins: model splits the server banner, renderer shows it --------

def test_model_takes_why_and_note_from_server_banner():
    js = _app_js()
    assert "function personSearchFirstSentence(banner)" in js
    assert "function personSearchNoteLines(banner)" in js
    assert '"If this is you:"' in js
    assert '"If it\'s someone else:"' in js
    assert "psBanner" in js
    assert "why = [psFirst]" in js
    assert "personNote: psNote" in js


def test_card_renders_note_block_under_rows():
    js = _app_js()
    assert 'data-testid", "risk-person-note"' in js or \
        "'data-testid', 'risk-person-note'" in js or \
        '"risk-person-note"' in js
    assert "risk-person-note-line" in js
    assert "model.personNote" in js


def test_person_search_rule_wins_over_reidentification():
    js = _app_js()
    assert '"person-search"' in js
    assert '"reidentification"' in js
    assert "hasPersonRule" in js or "person-search" in js
    # The card filters the re-id row when both fired; the drawer keeps both
    # (riskRuleTitles still joins every id).
    assert "riskRuleTitles" in js
    head = js[js.index("function riskCardModel"):]
    filt = head.index("reidentification")
    assert "person-search" in head[max(0, filt - 600):filt + 600]


def test_no_banner_wording_duplicated_in_js():
    js = _app_js()
    for literal in (
        "it's your choice",
        "you can send it",
        "the AI provider receives",
        "may be logged",
        "expose them to harm",
        "names a person and gives details",
    ):
        assert literal not in js, literal


# --- behaviour: the real JS helpers on the real server banner ---------------

NODE_PROBE = r"""
"use strict";
const fs = require("fs");
const banner = fs.readFileSync(process.argv[2], "utf8");
const src = fs.readFileSync("src/shadowfenced/static/app.js", "utf8");

function extractFn(name) {
  const start = src.indexOf("function " + name + "(");
  if (start < 0) throw new Error("missing " + name);
  let i = src.indexOf("{", start);
  let depth = 0;
  let inS = null, inC = null;
  for (let j = i; j < src.length; j++) {
    const c = src[j], n = src[j + 1];
    if (inC === "//") { if (c === "\n") inC = null; continue; }
    if (inC === "/*") { if (c === "*" && n === "/") { inC = null; j++; } continue; }
    if (inS) {
      if (c === "\\") { j++; continue; }
      if (c === inS) inS = null;
      continue;
    }
    if (c === "/" && n === "/") { inC = "//"; continue; }
    if (c === "/" && n === "*") { inC = "/*"; j++; continue; }
    if (c === "'" || c === '"' || c === "`") { inS = c; continue; }
    if (c === "{") depth++;
    if (c === "}") { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}
function extractVar(name) {
  const start = src.indexOf("var " + name + " =");
  if (start < 0) throw new Error("missing " + name);
  let i = src.indexOf("{", start);
  let depth = 0;
  let inS = null, inC = null;
  for (let j = i; j < src.length; j++) {
    const c = src[j], n = src[j + 1];
    if (inC === "//") { if (c === "\n") inC = null; continue; }
    if (inC === "/*") { if (c === "*" && n === "/") { inC = null; j++; } continue; }
    if (inS) {
      if (c === "\\") { j++; continue; }
      if (c === inS) inS = null;
      continue;
    }
    if (c === "/" && n === "/") { inC = "//"; continue; }
    if (c === "/" && n === "*") { inC = "/*"; j++; continue; }
    if (c === "'" || c === '"' || c === "`") { inS = c; continue; }
    if (c === "{") depth++;
    if (c === "}") {
      depth--;
      if (depth === 0) {
        let k = j + 1;
        while (src[k] === " " || src[k] === "\t" || src[k] === "\n" || src[k] === "\r") k++;
        return src.slice(start, (src[k] === ";" ? k + 1 : j + 1));
      }
    }
  }
  throw new Error("unbalanced var " + name);
}

// Minimal DOM for the risk-card renderer.
class El {
  constructor(tag) {
    this.tagName = tag; this.children = [];
    this.attributes = {}; this.textContent = "";
    this._html = ""; this.hidden = false;
  }
  set className(v) { this._class = String(v); }
  get className() { return this._class || ""; }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  setAttribute(k, v) { this.attributes[String(k)] = String(v); }
  addEventListener() {}
  appendChild(c) { this.children.push(c); return c; }
  insertBefore(c) { this.children.push(c); return c; }
  querySelector() { return null; }
}
function textOf(el) {
  let out = el.textContent || "";
  if (el._html) out += " " + el._html;
  for (const c of el.children || []) out += " " + textOf(c);
  return out;
}
global.document = { createElement: (t) => new El(t), addEventListener: () => {} };
global.CustomEvent = class { constructor(n, o) { this.type = n; this.detail = o; } };

const order = [
  "RISK_PLACEHOLDER_LABELS", "RISK_REID_LABELS", "RISK_HUMAN_LABELS",
];
let prelude = "";
for (const v of order) prelude += extractVar(v) + "\n";
const fns = [
  "riskPlaceholderBase", "riskLabelForBase", "riskArticle", "riskJoinLabels",
  "riskHumanLabel", "riskSpanLabel", "riskActionFor",
  "personSearchFirstSentence", "personSearchNoteLines",
  "riskCardModel", "riskRuleRow", "riskCardHead",
];
let code = prelude;
for (const f of fns) code += extractFn(f) + "\n";
code += "\nglobal.highlightPlaceholders = (t) => String(t);";
code += "\nglobal.riskCardModel = riskCardModel;";
code += "\nglobal.riskCardHead = riskCardHead;global.riskRuleRow = riskRuleRow;";
eval(code);
const riskCardModel = global.riskCardModel;
const riskCardHead = global.riskCardHead;

const catalog = {
  "person-search": { id: "person-search", title: "Person search" },
  "reidentification": { id: "reidentification", title: "Combined identifying details" },
};
const resp = {
  decision: "WARN",
  rule_ids: ["reidentification", "person-search"],
  sent_text: "Find everything about Daniel Whitmore, born 14 March 1987, lives in Leeds",
  evidence: {
    person_search: { banner: banner },
    reid: { combination: ["name", "dob", "city"] },
  },
  trace: [],
};
const model = riskCardModel(resp, catalog);
const whyRow = (model.rows || []).filter((r) => r.id === "why").map((r) => r.text).join(" ");
const card = new El("div");
riskCardHead(card, model);
const dom = textOf(card);
const ruleRows = [];
(function walk(n) {
  if (n.attributes && n.attributes["data-testid"] === "risk-rule") ruleRows.push(textOf(n));
  (n.children || []).forEach(walk);
})(card);
console.log("PROBE " + JSON.stringify({
  why: whyRow,
  note: model.personNote || [],
  rules: (model.rules || []).map((r) => r.id),
  dom: dom,
  ruleRows: ruleRows,
}));
"""


def test_card_model_and_dom_use_server_text(tmp_path):
    if shutil.which("node") is None:
        import pytest as _pytest

        _pytest.skip("node is not available")
    banner_file = tmp_path / "banner.txt"
    banner_file.write_text(BANNER, encoding="utf-8")
    probe = tmp_path / "t62c-probe.js"
    probe.write_text(NODE_PROBE, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(probe), str(banner_file)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = next(
        line for line in proc.stdout.splitlines() if line.startswith("PROBE ")
    )
    got = json.loads(line[len("PROBE "):])

    # Why row: the first server sentence, not the generic re-id wording.
    assert got["why"] == FIRST_SENTENCE
    assert "Together these identify you" not in got["why"]

    # Note block: the two server sentences, split from the banner.
    assert len(got["note"]) == 2
    assert got["note"][0].startswith("If this is you:")
    assert got["note"][1].startswith("If it's someone else:")
    assert got["note"][0] in BANNER
    assert got["note"][1] in BANNER

    # One rule row: Person search wins when both fired.
    assert got["rules"] == ["person-search"]
    assert len(got["ruleRows"]) == 1
    assert "Person search" in got["ruleRows"][0]

    # Rendered DOM carries the server text.
    assert FIRST_SENTENCE in got["dom"]
    assert "If this is you" in got["dom"]
