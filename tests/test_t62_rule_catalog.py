"""T62: one rule catalogue, rule ids on decisions, panel and card wiring."""

from __future__ import annotations

import json
from pathlib import Path

from shadowfenced import tier1
from shadowfenced.app import SendRequest, send
from shadowfenced.canary import CanaryRegistry
from shadowfenced.engine import gate
from shadowfenced.policy import load_policy
from shadowfenced import rule_catalog as catalog_module
from shadowfenced.rules import get_rule_catalog

ROOT = Path(__file__).resolve().parents[1]
SEED = "test-seed-t62"
KINDS = ("db_password", "api_key")


def _policy():
    return load_policy(ROOT / "policy.yaml")


def _registry():
    return CanaryRegistry(SEED, KINDS)


def _entries():
    return catalog_module.get_catalog(_policy())


# --- catalogue / detector coverage, both ways ---


def test_catalog_entries_name_only_real_detectors():
    known = catalog_module.known_detectors()
    for entry in _entries():
        for detector in entry["detectors"]:
            assert detector in known, (entry["id"], detector)


def test_every_detector_has_a_catalogue_entry():
    covered = catalog_module.detector_coverage(_entries())
    missing = [name for name, ids in covered.items() if not ids]
    assert missing == []


def test_trace_steps_are_covered_by_the_catalogue():
    policy, registry = _policy(), _registry()
    trace: list = []
    tier1.scan("mail ada@corvanelabs.com about Project TALLOW", policy, registry, trace_out=trace)
    tier1.scan_reid("Dana Whitlock, born 14 March 1991", trace_out=trace)
    covered = catalog_module.detector_coverage(_entries())
    scope = ("tier1.", "reid.", "intent.", "doc.", "contract.")
    for step in trace:
        name = str(step.get("step", ""))
        if not name.startswith(scope):
            continue
        canonical = catalog_module.normalize_detector(name)
        assert canonical in covered, name
        assert covered[canonical], name


def test_catalog_entry_shape_actions_and_keeps():
    policy = _policy()
    body = get_rule_catalog()
    assert set(body) == {"entries", "footer"}
    assert body["footer"] == catalog_module.FOOTER
    assert len(body["entries"]) >= 10
    by_id = {}
    for entry in body["entries"]:
        assert set(entry) >= {
            "id", "title", "catches", "action", "can_send_anyway",
            "why", "related", "detectors",
        }, entry["id"]
        assert entry["id"] and entry["title"] and entry["catches"]
        assert entry["why"] and len(entry["why"].split()) >= 5, entry["id"]
        assert entry["action"] in ("BLOCK", "REDACT", "WARN", "HOLD"), entry["id"]
        assert isinstance(entry["can_send_anyway"], bool), entry["id"]
        assert entry["detectors"] and isinstance(entry["related"], list)
        assert entry["id"] not in by_id
        by_id[entry["id"]] = entry
    assert by_id["api-keys"]["action"] == policy.action_for("secret").upper()
    assert by_id["api-keys"]["can_send_anyway"] == policy.keepable("secret")
    assert by_id["personal-data"]["action"] == policy.action_for("pii").upper()
    assert by_id["business-details"]["action"] == policy.action_for("business").upper()
    assert by_id["suspicious-text"]["action"] == "HOLD"
    assert by_id["suspicious-text"]["can_send_anyway"] == policy.keepable("beacon")
    assert "GDPR" in " ".join(by_id["personal-data"]["related"])
    assert "PCI DSS" in " ".join(by_id["card-numbers"]["related"])
    assert "GLBA" in " ".join(by_id["bank-details"]["related"])
    assert "HIPAA" in " ".join(by_id["sensitive-documents"]["related"])
    assert "stalking" in " ".join(by_id["person-search"]["related"])
    assert "un-sent" in " ".join(by_id["api-keys"]["related"])


def test_footer_disclaimer_text():
    assert "not legal advice" in catalog_module.FOOTER
    assert "does not make you compliant" in catalog_module.FOOTER


# --- rule ids on the decision payload ---


def test_key_send_returns_key_rule():
    key = "s" + "k-" + "A" * 48
    decision = gate("do not share " + key + " with anyone", _policy(), _registry(), mode="send")
    assert decision.decision == "BLOCK"
    assert "api-keys" in decision.rule_ids
    body = send(SendRequest(messages=[{"role": "user", "content": "key " + key}]))
    assert body["decision"] == "BLOCK"
    assert "api-keys" in body["rule_ids"]


def _person_search_judge(text):
    return {
        "person_search": True,
        "subject_public": False,
        "subject": "Dana Whitlock",
        "inferred": ["Dana Whitlock", "14 March 1991"],
        "confidence": 0.9,
    }


def test_person_search_returns_rule_and_choice_wording():
    trace: list = []
    text = "Find everything you can about Dana Whitlock, born 14 March 1991."
    decision = gate(
        text, _policy(), _registry(), mode="send", trace_out=trace,
        person_search_judge_fn=_person_search_judge,
    )
    assert decision.decision == "WARN"
    assert "person-search" in decision.rule_ids
    banner = (decision.evidence or {}).get("person_search", {}).get("banner", "")
    assert "If this is you" in banner
    assert "Rule: Person search" in banner
    reasons = " ".join(decision.reasons)
    for raw in ("Dana Whitlock", "14 March 1991"):
        assert raw not in reasons, raw
        assert raw not in json.dumps(trace), raw


# --- panel, card and drawer wiring (static pins) ---


def _rules_js() -> str:
    return (ROOT / "src" / "shadowfenced" / "static" / "rules.js").read_text(encoding="utf-8")


def _app_js() -> str:
    return (ROOT / "src" / "shadowfenced" / "static" / "app.js").read_text(encoding="utf-8")


def test_rules_panel_lists_what_is_protected():
    js = _rules_js()
    assert "What Shadowfenced protects" in js
    assert "Your organisation's own words (from policy.yaml)." in js
    assert "/api/rules/catalog" in js
    assert "You can send anyway: " in js
    assert "Why this rule" in js
    assert "body.footer" in js
    assert 'data-testid", "sf-catalog"' in js or 'data-testid="sf-catalog"' in js


def test_catalog_route_is_registered():
    from shadowfenced.app import app as app_module

    paths = app_module.openapi()["paths"]
    assert "get" in paths["/api/rules/catalog"]
    assert "delete" in paths["/api/rules/{rule_id}"]


def test_rules_panel_footer_and_hosted_offset():
    js = _rules_js()
    assert "sf-catalog-footer" in js
    css = (ROOT / "src" / "shadowfenced" / "static" / "rules.css").read_text(encoding="utf-8")
    assert ".sf-overlay.sf-hosted" in css
    offset = css.split(".sf-overlay.sf-hosted")[1].split("}")[0]
    assert "top:" in offset
    assert "hosted-banner" in js


def test_risk_card_names_the_rule():
    js = _app_js()
    assert "resp.rule_ids" in js or "rule_ids" in js
    assert '"RULE " +' in js or "'RULE ' +" in js or "RULE \" +" in js or "RULE " in js
    assert "Why this rule" in js
    assert "risk-rule-why" in js
    assert "sf-open-rule" in js
    assert "Open in Rules panel" in js
    assert "Searching for a person" in js
    assert "/api/rules/catalog" in js


def test_details_drawer_has_rule_line():
    html = (ROOT / "src" / "shadowfenced" / "static" / "index.html").read_text(encoding="utf-8")
    assert "data-turn-rules" in html
    js = _app_js()
    assert "riskRuleTitles" in js
    assert '"RULE " +' in js or "'RULE ' +" in js
