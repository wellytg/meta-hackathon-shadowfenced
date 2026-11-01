"""Hosted demo mode (T25): banner, memory-only rules, caps, judge fallback.

No test touches the network: the Ollama probe is faked out and the
Meta judge answers through an httpx ``MockTransport``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from shadowfenced import app as app_module
from shadowfenced import holds, hosted as hosted_module
from shadowfenced import uploads
from shadowfenced.app import app, handle_upload
from shadowfenced.engine import Decision
from shadowfenced.policy import delete_custom_rule, load_custom_rules
from shadowfenced.rules import CreateRuleRequest, create_rule
from shadowfenced.spans import span_from_cp_range

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"

BANNER = "Public demo. Use made-up data only."
LABEL = "judge: Muse Spark (hosted demo)"


@pytest.fixture()
def hosted_env(monkeypatch, tmp_path):
    """Hosted demo on, hermetic policy file, key, and empty stores."""
    monkeypatch.setenv("SF_HOSTED", "1")
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("SF_LOCAL_POLICY", str(tmp_path / "policy.local.yaml"))
    monkeypatch.setenv("META_API_KEY", "test-meta-key")
    app_module.reset_state()
    hosted_module.reset_hosted_rules()
    hosted_module.reset_rate_limiter()
    uploads.reset_for_tests()
    holds.reset_for_tests()
    yield
    app_module.reset_state()
    hosted_module.reset_hosted_rules()
    hosted_module.reset_rate_limiter()
    uploads.reset_for_tests()
    holds.reset_for_tests()


def _meta_transport(payload: dict, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": json.dumps(payload)}}
                ]
            },
        )

    return httpx.MockTransport(handler)


def _benign_payload() -> dict:
    return {
        "cls": "benign",
        "is_beacon": False,
        "sensitivity": 0.1,
        "confidence": 0.9,
        "span": "",
    }


# --- banner -----------------------------------------------------------------


def test_banner_on_health_and_pages(hosted_env):
    body = app_module.hosted_status()
    assert body == {"hosted": True, "banner": BANNER}
    assert hosted_module.HOSTED_BANNER == BANNER
    console = (STATIC / "index.html").read_text(encoding="utf-8")
    splash = (STATIC / "welcome" / "index.html").read_text(encoding="utf-8")
    for page in (console, splash):
        assert BANNER in page
        assert 'id="hosted-banner"' in page
    assert '"/api/hosted"' in (STATIC / "app.js").read_text(encoding="utf-8")


def test_no_banner_locally(monkeypatch):
    monkeypatch.delenv("SF_HOSTED", raising=False)
    assert app_module.hosted_status() == {"hosted": False, "banner": None}


def test_startup_banner_printed(hosted_env, capsys):
    app_module.print_startup_banner()
    assert BANNER in capsys.readouterr().out


def test_no_startup_banner_locally(monkeypatch, capsys):
    monkeypatch.delenv("SF_HOSTED", raising=False)
    app_module.print_startup_banner()
    assert capsys.readouterr().out == ""


# --- rules: memory only, capped at 20 ----------------------------------------


def test_rules_memory_only_and_capped(hosted_env):
    target = Path(os.environ["SF_LOCAL_POLICY"])
    for index in range(20):
        create_rule(
            CreateRuleRequest(
                kind="codename", value=f"DEMO{index:02d}", action="redact"
            )
        )
    assert len(load_custom_rules()) == 20
    assert not target.exists()
    with pytest.raises(HTTPException) as caught:
        create_rule(CreateRuleRequest(kind="codename", value="ONE2MANY", action="redact"))
    assert caught.value.status_code == 422
    assert not target.exists()
    first = load_custom_rules()[0]
    assert delete_custom_rule(first.id) is True
    create_rule(CreateRuleRequest(kind="codename", value="FRESH01", action="redact"))
    assert len(load_custom_rules()) == 20
    assert not target.exists()


# --- uploads capped at 1 MB --------------------------------------------------


def test_upload_cap_hosted(hosted_env):
    big = b"a" * (hosted_module.HOSTED_MAX_FILE_BYTES + 1)
    result = handle_upload("big.txt", big)
    assert result["status"] == "unscannable"


def test_upload_at_cap_hosted(hosted_env):
    exact = b"a" * hosted_module.HOSTED_MAX_FILE_BYTES
    result = handle_upload("exact.txt", exact)
    assert result["status"] != "unscannable"


def test_same_upload_ok_locally(monkeypatch):
    monkeypatch.delenv("SF_HOSTED", raising=False)
    big = b"a" * (hosted_module.HOSTED_MAX_FILE_BYTES + 1)
    result = handle_upload("big.txt", big)
    assert result["status"] == "clean"
    uploads.reset_for_tests()


# --- per-IP rate limiting ----------------------------------------------------


def test_rate_limit_buckets_per_ip(hosted_env, monkeypatch):
    monkeypatch.setattr(hosted_module, "RATE_CAPACITY", 3.0)
    monkeypatch.setattr(hosted_module, "RATE_REFILL_PER_S", 1.0)
    hosted_module.reset_rate_limiter()
    moment = time.time()
    assert [hosted_module.allow_request("1.2.3.4", now=moment) for _ in range(3)] == [
        True,
        True,
        True,
    ]
    assert hosted_module.allow_request("1.2.3.4", now=moment) is False
    assert hosted_module.allow_request("5.6.7.8", now=moment) is True
    assert hosted_module.allow_request("1.2.3.4", now=moment + 5.0) is True


def _limited_request(host: str):
    return SimpleNamespace(client=SimpleNamespace(host=host))


def test_rate_limit_middleware_returns_429(hosted_env, monkeypatch):
    monkeypatch.setattr(hosted_module, "RATE_CAPACITY", 1.0)
    monkeypatch.setattr(hosted_module, "RATE_REFILL_PER_S", 0.0)
    hosted_module.reset_rate_limiter()
    assert app_module.hosted_limit_response(_limited_request("9.9.9.9")) is None
    limited = app_module.hosted_limit_response(_limited_request("9.9.9.9"))
    assert limited is not None and limited.status_code == 429
    dispatches = [entry.kwargs.get("dispatch") for entry in app.user_middleware]
    assert any(
        getattr(dispatch, "__name__", "") == "_hosted_rate_limit"
        for dispatch in dispatches
    )


def test_no_rate_limit_locally(monkeypatch):
    monkeypatch.delenv("SF_HOSTED", raising=False)
    monkeypatch.setattr(hosted_module, "RATE_CAPACITY", 1.0)
    monkeypatch.setattr(hosted_module, "RATE_REFILL_PER_S", 0.0)
    hosted_module.reset_rate_limiter()
    assert app_module.hosted_limit_response(_limited_request("9.9.9.9")) is None
    assert app_module.hosted_limit_response(_limited_request("9.9.9.9")) is None
    hosted_module.reset_rate_limiter()


# --- Tier 2 judge fallback ----------------------------------------------------


def test_fallback_uses_meta_when_ollama_unreachable(hosted_env, monkeypatch):
    monkeypatch.setattr(hosted_module, "ollama_reachable", lambda **kwargs: False)
    calls: list[httpx.Request] = []
    judgement, label = hosted_module.judge_for_review(
        "some window text",
        "muse-glimmer:30b",
        meta_transport=_meta_transport(_benign_payload(), calls),
    )
    assert label == LABEL
    assert judgement is not None and judgement.cls == "benign"
    assert "/chat/completions" in str(calls[0].url)
    assert calls[0].headers.get("authorization", "").startswith("Bearer ")


def test_local_mode_still_uses_ollama(monkeypatch):
    monkeypatch.delenv("SF_HOSTED", raising=False)
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "cls": "secret",
                            "is_beacon": False,
                            "sensitivity": 0.8,
                            "confidence": 0.7,
                            "span": "sekret",
                        }
                    ),
                }
            },
        )

    judgement, label = hosted_module.judge_for_review(
        "some window text", "m", judge_transport=httpx.MockTransport(handler)
    )
    assert label is None
    assert judgement is not None and judgement.cls == "secret"
    assert "/api/chat" in str(calls[0].url)


def test_hold_review_falls_back_and_labels_trace(hosted_env, monkeypatch):
    monkeypatch.setattr(hosted_module, "ollama_reachable", lambda **kwargs: False)
    text = "hello world, nothing sensitive here"
    span = span_from_cp_range(
        text,
        0,
        5,
        cls="business",
        subclass="cue",
        tier=1,
        proof="code",
        status="ambiguous",
        message="possible issue",
        why="needs review",
        suggested_action="review",
        can_keep=True,
        confidence=0.5,
    )
    decision = Decision(
        decision="HOLD",
        decision_id="hold-benign",
        spans=[span],
        sent_text=None,
        placeholders={},
        resolved_tier=4,
        latency_ms={},
        reasons=["ambiguous"],
        evidence={"pending": [{"kind": "message", "text": text}]},
        status="reviewing",
    )
    hold = holds.create_hold(
        decision, text, [{"role": "user", "content": text}], None
    )
    assert hold.status == "reviewing"
    result = holds.run_review(
        hold.hold_id,
        meta_transport=_meta_transport(_benign_payload()),
        sender=lambda sent: "ok answer",
    )
    assert result is not None and result.status == "released"
    assert result.evidence.get("judge") == LABEL
    assert any(
        entry.get("detail") == LABEL for entry in result.evidence.get("trace", [])
    )
    assert holds.hold_card(result)["judge"] == LABEL


def test_meta_verdict_goes_through_code_verification(hosted_env, monkeypatch):
    monkeypatch.setattr(hosted_module, "ollama_reachable", lambda **kwargs: False)
    text = "contact ada@corvanelabs.com today"
    decision = Decision(
        decision="HOLD",
        decision_id="hold-pii",
        spans=[],
        sent_text=None,
        placeholders={},
        resolved_tier=4,
        latency_ms={},
        reasons=["cue word"],
        evidence={"pending": [{"kind": "message", "text": text}]},
        status="reviewing",
    )
    hold = holds.create_hold(
        decision, text, [{"role": "user", "content": text}], None
    )
    assert hold.status == "reviewing"
    payload = {
        "cls": "pii",
        "is_beacon": False,
        "sensitivity": 0.9,
        "confidence": 0.95,
        "span": "ada@corvanelabs.com",
    }
    result = holds.run_review(
        hold.hold_id,
        meta_transport=_meta_transport(payload),
        sender=lambda sent: "noted [PII_EMAIL_1], thanks",
    )
    assert result is not None and result.status == "redacted_sent"
    assert "ada@corvanelabs.com" not in (result.sent_text or "")
    assert result.evidence.get("judge") == LABEL
