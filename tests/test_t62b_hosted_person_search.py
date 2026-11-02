"""T62b: hosted person-search, code fallback, rule labels, reasons, counters, pairing."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from shadowfenced import hosted as hosted_module
from shadowfenced import person_search as ps_module
from shadowfenced.canary import CanaryRegistry
from shadowfenced.engine import gate
from shadowfenced.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
SEED = "test-seed-t62b"
KINDS = ("db_password", "api_key")
STATIC = ROOT / "src" / "shadowfenced" / "static"

DANIEL = "Find everything about Daniel Whitmore, born 14 March 1987, lives in Leeds"
ADA = "Find everything about Ada Lovelace, born 10 December 1815 in London."


def _policy():
    return load_policy(ROOT / "policy.yaml")


def _registry():
    return CanaryRegistry(SEED, KINDS)


def _meta_transport(verdict: dict) -> httpx.MockTransport:
    body = json.dumps(verdict)

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200, json={"choices": [{"message": {"content": body}}]}
            )
        return httpx.Response(500, json={})

    return httpx.MockTransport(_handle)


def test_hosted_person_search_uses_spark_judge(monkeypatch):
    monkeypatch.setenv("SF_HOSTED", "1")
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("META_API_KEY", "test-key-123")
    monkeypatch.setattr(hosted_module, "ollama_reachable", lambda **kw: False)
    verdict = {
        "person_search": True,
        "subject_public": False,
        "subject": "Daniel Whitmore",
        "inferred": ["Daniel Whitmore", "14 March 1987"],
        "confidence": 0.9,
    }
    result = ps_module.check_person_search(
        DANIEL, transport=_meta_transport(verdict)
    )
    assert result.warn is True
    assert "If this is you" in result.banner
    assert "Rule: Person search." in result.banner
    assert "Daniel Whitmore" not in result.banner
    assert "14 March 1987" not in result.banner


def test_hosted_spark_failure_falls_back_to_code(monkeypatch):
    monkeypatch.setenv("SF_HOSTED", "1")
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("META_API_KEY", "test-key-123")
    monkeypatch.setattr(hosted_module, "ollama_reachable", lambda **kw: False)

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("meta down")

    trace: list = []
    result = ps_module.check_person_search(
        DANIEL, transport=httpx.MockTransport(_boom), trace_out=trace
    )
    assert result.warn is True
    assert "If this is you" in result.banner
    assert result.reason.startswith("code fallback")
    step = next(s for s in trace if s["step"] == "intent.person_search")
    assert step["tool"] == "code"
    assert step["result"] == "WARN"
    assert "Daniel Whitmore" not in step["detail"]
    assert "14 March 1987" not in step["detail"]


def test_code_fallback_warns_with_rule_and_trace(monkeypatch):
    monkeypatch.delenv("SF_HOSTED", raising=False)
    monkeypatch.setenv("SF_TIER2_MODE", "off")
    trace: list = []
    decision = gate(
        DANIEL, _policy(), _registry(), mode="send", trace_out=trace
    )
    assert decision.decision == "WARN"
    person = (decision.evidence or {}).get("person_search")
    assert person is not None
    assert "If this is you" in person["banner"]
    assert "person-search" in decision.rule_ids
    blob = " ".join(decision.reasons) + json.dumps(trace)
    assert "Daniel Whitmore" not in blob
    assert "14 March 1987" not in blob
    step = next(s for s in trace if s["step"] == "intent.person_search")
    assert step["tool"] == "code"
    assert step["result"] == "WARN"


def test_public_figure_still_not_flagged(monkeypatch):
    monkeypatch.delenv("SF_HOSTED", raising=False)
    monkeypatch.setenv("SF_TIER2_MODE", "off")
    decision = gate(ADA, _policy(), _registry())
    assert "person_search" not in (decision.evidence or {})
    assert decision.decision != "WARN" or "person_search" not in (
        decision.evidence or {}
    )


def test_card_and_drawer_rule_value_has_no_leading_rule():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '"RULE " +' not in js
    assert "'RULE ' +" not in js
    assert "return (found && found.title" in js
    assert "Why this rule" in js


def test_hosted_reason_names_spark_local_names_glimmer(monkeypatch, tmp_path):
    monkeypatch.setenv("SF_LOG_PATH", str(tmp_path / "d.jsonl"))
    monkeypatch.setenv("SF_TIER2_MODE", "off")
    monkeypatch.delenv("SF_HOSTED", raising=False)
    local = gate("born", _policy(), _registry(), mode="send")
    assert any("Glimmer" in reason for reason in local.reasons)
    monkeypatch.setenv("SF_HOSTED", "1")
    hosted = gate("born", _policy(), _registry(), mode="send")
    assert any("Muse Spark" in reason for reason in hosted.reasons)
    assert not any("Glimmer" in reason for reason in hosted.reasons)


def test_counters_refresh_after_decision_and_warn_resolve():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '["ALLOW", "REDACT", "BLOCK", "HOLD", "WARN"]' in js
    note = js[js.index("function noteTurn"):]
    assert "refreshStats();" in note.split("function paintTurn")[0]
    confirm = js[js.index("function riskConfirmWarn"):]
    assert "refreshStats();" in confirm


def test_no_pairing_fetch_when_hosted():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "loadExtTokenDeferred" in js
    guard = js[js.index("function loadExtToken("):]
    guard = guard[: guard.index("/api/ext/pairing")]
    assert "hostedMode" in guard
