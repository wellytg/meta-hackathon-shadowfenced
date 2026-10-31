"""Stats tests (T7): counters, tiers, p50/p95, and the /api/stats route."""

import httpx

from shadowfenced import ecs_log, engine
from shadowfenced.app import app, log_verify, send, stats
from shadowfenced.app import SendRequest
from shadowfenced.canary import CanaryRegistry
from shadowfenced.stats import Stats, stats_from_log

SEED = "test-seed-t7"
ENTROPY_TOKEN = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"


def _gate(text):
    return engine.gate(text, load_policy(), registry())


def load_policy():
    from shadowfenced.policy import load_policy as _load

    return _load()


def registry():
    return CanaryRegistry(SEED, ("db_password",))


def _mock_transport(monkeypatch, calls, answer="ok"):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": answer}}]},
        )

    monkeypatch.setattr(
        app.state, "upstream_transport", httpx.MockTransport(handler), raising=False
    )


def test_stats_counters_and_percentiles():
    collector = Stats()
    for ms in (10.0, 20.0, 30.0, 40.0):
        collector.record("ALLOW", 1, ms)
    collector.record("HOLD", 1, 50.0)
    snap = collector.snapshot()
    assert snap["total"] == 5
    assert snap["decisions"] == {"ALLOW": 4, "HOLD": 1}
    assert snap["by_tier"] == {"1": 5}
    assert snap["last_latency_ms"] == {"1": 50.0}
    assert snap["p50_latency_ms"] == {"1": 30.0}
    assert snap["p95_latency_ms"] == {"1": 50.0}


def test_stats_from_log_counts_and_tiers(tmp_path, monkeypatch):
    log = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("SF_LOG_PATH", str(log))
    ecs_log.append_decision(None, _gate("hello there"), "hello there")
    ecs_log.append_decision(
        None, _gate("mail ada@corvanelabs.com now"), "mail ada@corvanelabs.com now"
    )
    ecs_log.append_decision(
        None, _gate(f"maybe {ENTROPY_TOKEN}"), f"maybe {ENTROPY_TOKEN}"
    )
    snap = stats_from_log()
    assert snap["total"] == 3
    assert snap["decisions"] == {"ALLOW": 1, "REDACT": 1, "HOLD": 1}
    assert snap["by_tier"] == {"1": 3}
    assert set(snap) == {
        "total", "decisions", "by_tier",
        "last_latency_ms", "p50_latency_ms", "p95_latency_ms",
        "held_then_resolved",
    }
    assert snap["held_then_resolved"] == 0


def test_stats_and_verify_routes_reflect_logged_sends(monkeypatch):
    calls: list = []
    _mock_transport(monkeypatch, calls)
    send(SendRequest(messages=[{"role": "user", "content": "hello there"}]))
    send(
        SendRequest(
            messages=[{"role": "user", "content": "mail ada@corvanelabs.com now"}]
        )
    )
    snap = stats()
    assert snap["total"] == 2
    assert snap["decisions"]["ALLOW"] == 1
    assert snap["decisions"]["REDACT"] == 1
    result = log_verify()
    assert result == {"ok": True, "events": 2}
