"""Eval tests (T15): metric math and the report schema.

No real model calls: the judge is an httpx MockTransport returning canned
verdicts, and the upstream sender is a dry stub. Entropy tokens and fake
keys are minted at runtime from a seed.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shadowfenced import corpus  # noqa: E402
from shadowfenced.canary import CanaryRegistry  # noqa: E402
from shadowfenced.policy import load_policy  # noqa: E402

import eval as eval_module  # noqa: E402

SEED = "test-seed-eval"
POLICY_PATH = Path(__file__).resolve().parents[1] / "policy.yaml"


def _harness():
    policy = load_policy(POLICY_PATH)
    registry = CanaryRegistry(SEED, ("db_password", "api_key"))
    return policy, registry


def _row(case_id, category, gate, final, tier, leaked,
         gate_ms=1.0, review_ms=0.0):
    return {
        "id": case_id,
        "category": category,
        "expect": final,
        "acceptable": [final],
        "gate": gate,
        "final": final,
        "tier": tier,
        "meets_expect": True,
        "leaked": leaked,
        "gate_ms": gate_ms,
        "review_ms": review_ms,
        "outbound_chars": 10,
    }


def test_metric_math_on_toy_rows():
    rows = [
        _row("s1", "secrets", "BLOCK", "BLOCK", 1, False, gate_ms=10.0),
        _row("s2", "secrets", "HOLD", "REDACT", 3, False,
             gate_ms=20.0, review_ms=5.0),
        _row("s3", "secrets", "HOLD", "ALLOW", 3, True,
             gate_ms=30.0, review_ms=7.0),
        _row("n1", "benign", "ALLOW", "ALLOW", 1, False, gate_ms=40.0),
        _row("n2", "benign", "ALLOW", "REDACT", 1, False, gate_ms=50.0),
    ]
    metrics = eval_module.compute_metrics(rows)
    assert metrics["n"] == 5
    assert metrics["n_sensitive"] == 3
    assert metrics["n_benign"] == 2
    assert metrics["leak_rate"] == round(1 / 3, 4)
    assert metrics["catch_rate"] == round(2 / 3, 4)
    assert metrics["false_escalation_rate"] == 0.5
    assert metrics["hold_rate"] == 0.4
    assert metrics["human_rate"] == 0.0
    assert metrics["resolved_by_tier"] == {"1": 0.6, "3": 0.4, "4": 0.0}
    tier1 = metrics["latency_ms"]["1"]
    assert tier1["n"] == 3
    assert tier1["max"] == 50.0
    assert tier1["p50"] == 40.0
    assert tier1["p95"] == 50.0
    tier3 = metrics["latency_ms"]["3"]
    assert tier3["n"] == 2
    assert set(metrics["latency_ms"]) == {"1", "3", "4"}


def _fake_judge_transport(token_allow: str, token_redact: str):
    """Canned judge: benign-clear for one token, secret-confirm the other."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        window = body["messages"][1]["content"]
        if token_redact in window:
            verdict = {
                "cls": "secret",
                "is_beacon": False,
                "sensitivity": 0.9,
                "confidence": 0.9,
                "span": token_redact,
            }
        else:
            verdict = {
                "cls": "benign",
                "is_beacon": False,
                "sensitivity": 0.1,
                "confidence": 0.95,
                "span": token_allow,
            }
        return httpx.Response(
            200, json={"message": {"content": json.dumps(verdict)}})

    return httpx.MockTransport(handler)


def _toy_cases(token_allow: str, token_redact: str):
    canary = CanaryRegistry(SEED, ("db_password",)).value("db_password")
    return [
        {"id": "t-block", "category": "secrets", "input": f"rotate {canary}",
         "expect": "BLOCK", "acceptable": ["BLOCK"], "classes": ["secret"],
         "sensitive_values": [canary]},
        {"id": "t-release", "category": "secrets",
         "input": f"saw token {token_allow} here",
         "expect": "HOLD", "acceptable": ["HOLD", "ALLOW"],
         "classes": ["secret"], "sensitive_values": [token_allow]},
        {"id": "t-confirm", "category": "secrets",
         "input": f"saw token {token_redact} here",
         "expect": "HOLD", "acceptable": ["HOLD", "REDACT"],
         "classes": ["secret"], "sensitive_values": [token_redact]},
        {"id": "t-clean", "category": "benign", "input": "hello world",
         "expect": "ALLOW", "acceptable": ["ALLOW"], "classes": [],
         "sensitive_values": []},
    ]


def test_end_to_end_with_fake_judge(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    rng = random.Random("test-seed-eval-judge")
    token_allow = corpus.entropy_token(32, rng)
    token_redact = corpus.entropy_token(32, rng)
    policy, registry = _harness()
    rows = eval_module.evaluate_cases(
        _toy_cases(token_allow, token_redact),
        seed=SEED, policy=policy, registry=registry,
        judge_transport=_fake_judge_transport(token_allow, token_redact),
    )
    by_id = {row["id"]: row for row in rows}
    assert by_id["t-block"]["final"] == "BLOCK"
    assert by_id["t-block"]["tier"] == 1
    assert by_id["t-block"]["leaked"] is False
    assert by_id["t-release"]["final"] == "ALLOW"
    assert by_id["t-release"]["tier"] == 3
    assert by_id["t-release"]["leaked"] is True
    assert by_id["t-confirm"]["final"] == "REDACT"
    assert by_id["t-confirm"]["tier"] == 3
    assert by_id["t-confirm"]["leaked"] is False
    assert by_id["t-clean"]["final"] == "ALLOW"
    metrics = eval_module.compute_metrics(rows)
    assert metrics["leak_rate"] == round(1 / 3, 4)
    assert metrics["catch_rate"] == round(2 / 3, 4)
    assert metrics["false_escalation_rate"] == 0.0
    assert metrics["hold_rate"] == 0.5
    config = {"judge": "fake", "judge_model": None, "seed": SEED,
              "git_sha": "test", "date": "2026-01-01", "keep_alive": "5m"}
    report = eval_module.build_report(rows, metrics, config)
    assert set(report) == {"config", "metrics", "per_case"}
    assert set(report["config"]) == {
        "judge", "judge_model", "seed", "git_sha", "date", "keep_alive"}
    assert set(report["metrics"]) == {
        "n", "n_sensitive", "n_benign", "leak_rate", "catch_rate",
        "false_escalation_rate", "hold_rate", "human_rate",
        "resolved_by_tier", "latency_ms"}
    row = report["per_case"][0]
    assert set(row) == {
        "id", "category", "expect", "acceptable", "gate", "final", "tier",
        "meets_expect", "leaked", "gate_ms", "review_ms", "outbound_chars"}


def test_hold_without_judge_stays_human_and_unleaked(monkeypatch):
    monkeypatch.setenv("SF_TIER2_MODE", "off")
    rng = random.Random("test-seed-eval-off")
    token = corpus.entropy_token(32, rng)
    policy, registry = _harness()
    rows = eval_module.evaluate_cases(
        [{"id": "t-hold", "category": "secrets",
          "input": f"saw token {token} here",
          "expect": "HOLD", "acceptable": ["HOLD"],
          "classes": ["secret"], "sensitive_values": [token]}],
        seed=SEED, policy=policy, registry=registry,
    )
    assert rows[0]["gate"] == "HOLD"
    assert rows[0]["final"] == "HOLD"
    assert rows[0]["tier"] == 4
    assert rows[0]["leaked"] is False
    metrics = eval_module.compute_metrics(rows)
    assert metrics["human_rate"] == 1.0
    assert metrics["resolved_by_tier"] == {"1": 0.0, "3": 0.0, "4": 1.0}
