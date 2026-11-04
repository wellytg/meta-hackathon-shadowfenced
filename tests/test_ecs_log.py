"""ECS decision log tests (T7): chain verifies, tamper detected, no raw values."""

import json

from shadowfenced import ecs_log, engine
from shadowfenced.canary import CanaryRegistry
from shadowfenced.policy import load_policy

SEED = "test-seed-t7"
EMAIL_TEXT = "mail ada@corvanelabs.com now"
RAW_EMAIL = "ada@corvanelabs.com"


def _gate(text):
    return engine.gate(text, load_policy(), CanaryRegistry(SEED, ("db_password",)))


def test_chain_verifies(tmp_path):
    log = str(tmp_path / "decisions.jsonl")
    ecs_log.append_decision(log, _gate(EMAIL_TEXT), EMAIL_TEXT)
    ecs_log.append_decision(log, _gate("hello there"), "hello there")
    assert ecs_log.verify(log) == (True, 2)
    assert ecs_log.verify_chain(log) is True


def test_verify_missing_file_is_empty_chain(tmp_path):
    assert ecs_log.verify(str(tmp_path / "absent.jsonl")) == (True, 0)


def test_tamper_detected(tmp_path):
    log = str(tmp_path / "decisions.jsonl")
    ecs_log.append_decision(log, _gate(EMAIL_TEXT), EMAIL_TEXT)
    lines = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["decision"] = "ALLOW"  # rewrite history without fixing the hash
    (tmp_path / "decisions.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    ok, _ = ecs_log.verify(log)
    assert ok is False
    assert ecs_log.verify_chain(log) is False


def test_no_raw_values_and_shape(tmp_path):
    log = str(tmp_path / "decisions.jsonl")
    record = ecs_log.append_decision(log, _gate(EMAIL_TEXT), EMAIL_TEXT)
    assert set(record) == {
        "event", "ts", "decision_id", "decision", "tier", "classes",
        "kinds", "rule_ids", "cue", "site", "source",
        "fingerprints", "domain_prints", "latency_ms", "prev_hash", "hash",
    }
    assert record["event"] == "decision"
    assert record["site"] == "app"
    assert record["source"] == "text"
    raw = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8")
    assert RAW_EMAIL not in raw
    assert ecs_log.fingerprint(RAW_EMAIL) in raw
    assert record["fingerprints"] == [ecs_log.fingerprint(RAW_EMAIL)]
    assert record["classes"] == ["pii"]
    assert record["kinds"] == ["pii:email"]
    assert len(record["domain_prints"]) == 1
    assert "corvanelabs.com" not in raw


def test_chain_links_prev_hash(tmp_path):
    log = str(tmp_path / "decisions.jsonl")
    first = ecs_log.append_decision(log, _gate(EMAIL_TEXT), EMAIL_TEXT)
    second = ecs_log.append_decision(log, _gate("hello there"), "hello there")
    assert second["prev_hash"] == first["hash"]
    assert first["prev_hash"] == ecs_log.GENESIS_HASH
