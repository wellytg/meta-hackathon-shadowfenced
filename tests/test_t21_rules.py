"""T21 tests: custom rules API + validation, engine effect, trace hygiene.

Custom rules live in a temp local-policy file (``SF_LOCAL_POLICY``), so no
test touches the real ``policy.local.yaml`` and the app policy cache is
reset around each test that writes rules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from shadowfenced import app as app_module
from shadowfenced.app import ScanRequest, SendRequest, scan, send
from shadowfenced.engine import gate
from shadowfenced.policy import (
    add_custom_rule,
    load_custom_rules,
    load_policy,
    validate_custom_rule_value,
)
from shadowfenced.rules import (
    CreateRuleRequest,
    create_rule,
    delete_rule,
    list_rules,
)

BASE_POLICY = Path(__file__).resolve().parents[1] / "policy.yaml"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def local_policy(monkeypatch, tmp_path):
    """Point the local rule file at temp storage with a fresh app cache."""
    target = tmp_path / "policy.local.yaml"
    monkeypatch.setenv("SF_LOCAL_POLICY", str(target))
    app_module.reset_state()
    yield target
    app_module.reset_state()


def _fresh_policy():
    return load_policy(BASE_POLICY)


# --- validation -----------------------------------------------------------


def test_codename_length_bounds():
    with pytest.raises(ValueError):
        validate_custom_rule_value("codename", "x")
    with pytest.raises(ValueError):
        validate_custom_rule_value("client", "y" * 65)
    assert validate_custom_rule_value("codename", "AB") == "AB"
    assert validate_custom_rule_value("client", "x" * 64) == "x" * 64


def test_pattern_must_compile_and_fit():
    with pytest.raises(ValueError):
        validate_custom_rule_value("pattern", "(unclosed")
    with pytest.raises(ValueError):
        validate_custom_rule_value("pattern", "a" * 201)
    assert validate_custom_rule_value("pattern", r"PRJ-\d{4}") == r"PRJ-\d{4}"


def test_pattern_nested_quantifiers_rejected():
    for bad in ("(a+)+", "(x*)*", "(a{2,3})+", "(?:ab+)+"):
        with pytest.raises(ValueError):
            validate_custom_rule_value("pattern", bad)
    # Plain groups and lone quantifiers are fine.
    assert validate_custom_rule_value("pattern", "(ab)+") == "(ab)+"
    assert validate_custom_rule_value("pattern", "a+") == "a+"


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        validate_custom_rule_value("email", "whatever")


def test_request_model_rejects_bad_kind_and_action():
    with pytest.raises(ValidationError):
        CreateRuleRequest(kind="email", value="AB", action="redact")
    with pytest.raises(ValidationError):
        CreateRuleRequest(kind="codename", value="AB", action="delete")


def test_create_rule_rejects_bad_value_with_422(local_policy):
    with pytest.raises(HTTPException) as caught:
        create_rule(CreateRuleRequest(kind="codename", value="x", action="redact"))
    assert caught.value.status_code == 422
    with pytest.raises(HTTPException) as caught:
        create_rule(
            CreateRuleRequest(kind="pattern", value="(a+)+", action="block")
        )
    assert caught.value.status_code == 422


# --- API ------------------------------------------------------------------


def test_get_rules_lists_base_and_empty_custom(local_policy):
    body = list_rules()
    kinds = {(entry["kind"], entry["value"]) for entry in body["base"]}
    assert ("codename", "TALLOW") in kinds
    assert ("client", "Aster Freight") in kinds
    assert all(entry["readonly"] for entry in body["base"])
    assert body["custom"] == []


def test_post_get_delete_round_trip(local_policy):
    created = create_rule(
        CreateRuleRequest(
            kind="client", value="Northwind Traders",
            action="redact", note="Q3 deal",
        )
    )
    assert created["id"]
    assert created["source"] == "custom"
    assert created["readonly"] is False
    assert local_policy.is_file()

    body = list_rules()
    assert [entry["id"] for entry in body["custom"]] == [created["id"]]
    assert load_custom_rules(local_policy)[0].value == "Northwind Traders"

    assert delete_rule(created["id"]) == {"deleted": created["id"]}
    assert list_rules()["custom"] == []

    with pytest.raises(HTTPException) as caught:
        delete_rule(created["id"])
    assert caught.value.status_code == 404


def test_custom_rule_applies_without_restart(local_policy):
    created = create_rule(
        CreateRuleRequest(kind="codename", value="ZEPHYR", action="redact")
    )
    # The cached app policy was reset, so the next load sees the rule.
    assert created["id"] in {
        rule.id for rule in app_module.get_policy().custom_rules
    }


def test_rules_routes_registered_before_static_mount():
    found = {
        (getattr(r, "path", ""), method)
        for r in app_module.app.routes
        for method in sorted(getattr(r, "methods", []) or [])
    }
    assert ("/api/rules", "GET") in found
    assert ("/api/rules", "POST") in found
    assert ("/api/rules/{rule_id}", "DELETE") in found


def test_gitignore_covers_local_policy():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "policy.local.yaml" in lines


# --- engine effect --------------------------------------------------------


def test_custom_codename_redacts(local_policy):
    add_custom_rule("codename", "ZEPHYR", "redact")
    decision = gate("discussing ZEPHYR timelines", _fresh_policy())
    assert decision.decision == "REDACT"
    assert decision.sent_text is not None
    assert "ZEPHYR" not in decision.sent_text
    assert "[CODENAME_1]" in decision.sent_text
    custom = [s for s in decision.spans if s.proof == "custom"]
    assert len(custom) == 1
    assert all(s.status == "proven" and s.tier == 1 for s in custom)


def test_custom_rule_block_action(local_policy):
    add_custom_rule("codename", "ZEPHYR", "block")
    decision = gate("discussing ZEPHYR timelines", _fresh_policy())
    assert decision.decision == "BLOCK"
    assert decision.sent_text is None


def test_custom_rule_review_action_holds(local_policy):
    add_custom_rule("client", "Northwind Traders", "review")
    tier1_decision = gate(
        "meeting Northwind Traders tomorrow", _fresh_policy()
    )
    assert tier1_decision.decision == "HOLD"
    send_decision = gate(
        "meeting Northwind Traders tomorrow", _fresh_policy(), mode="send"
    )
    assert send_decision.decision == "HOLD"
    assert send_decision.status == "reviewing"


def test_custom_pattern_rule(local_policy):
    add_custom_rule("pattern", r"PRJ-\d{4}", "redact")
    decision = gate("see PRJ-4821 today", _fresh_policy())
    assert decision.decision == "REDACT"
    assert decision.sent_text is not None
    assert "PRJ-4821" not in decision.sent_text


# --- descriptive trace ----------------------------------------------------


def test_trace_shape_and_no_raw_values(local_policy):
    add_custom_rule("codename", "ZEPHYR", "redact")
    secret = "ada@corvanelabs.com"
    text = f"mail {secret} about ZEPHYR"
    trace: list = []
    decision = gate(text, _fresh_policy(), trace_out=trace)
    assert decision.decision == "REDACT"
    assert trace
    for step in trace:
        assert set(step) == {"step", "tool", "detail", "result", "ms"}
        assert isinstance(step["ms"], (int, float))
    assert trace[-1]["step"] == "decision"
    assert trace[-1]["result"] == "REDACT"
    dumped = json.dumps(trace)
    assert secret not in dumped
    assert "ZEPHYR" not in dumped
    assert "[PII_EMAIL_1]" in dumped


def test_scan_api_includes_clean_trace(local_policy):
    body = scan(ScanRequest(text="mail ada@corvanelabs.com now"))
    assert isinstance(body["trace"], list)
    assert body["trace"][-1]["step"] == "scan.complete"
    dumped = json.dumps(body["trace"])
    assert "ada@corvanelabs.com" not in dumped


def test_send_api_includes_trace_and_keeps_log_clean(local_policy):
    add_custom_rule("codename", "ZEPHYR", "block")
    body = send(
        SendRequest(messages=[{"role": "user", "content": "ZEPHYR slips"}])
    )
    assert body["decision"] == "BLOCK"
    assert body["trace"]
    assert body["trace"][-1] == {
        "step": "decision",
        "tool": "gate",
        "detail": "resolved tier 1",
        "result": "BLOCK",
        "ms": body["trace"][-1]["ms"],
    }
    assert "ZEPHYR" not in json.dumps(body["trace"])
    from shadowfenced.config import get_settings

    log_lines = Path(get_settings().log_path).read_text(encoding="utf-8")
    assert "tier1.canary_scan" not in log_lines
