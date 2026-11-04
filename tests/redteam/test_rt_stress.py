"""Red-team: very long input and many spans (T79).

The gate must fail closed, never crash, and never leak -- however long the
input. Sizes here stay CI-friendly; a 5 MB input was additionally probed by
hand (correct verdict, no crash, but ~6 minutes: no input cap, see
``docs/security/REDTEAM_RESULTS.md``).
"""

from __future__ import annotations

import time

import pytest

from shadowfenced import engine
from helpers_rt import email, make_policy, make_registry


@pytest.fixture()
def policy():
    return make_policy()


@pytest.fixture()
def registry():
    return make_registry()

# Generous wall-clock bounds: they catch hangs, not slowness.
TIME_BUDGET_S = 120.0


def test_many_emails_redact_without_leak(policy, registry):
    """STOPPED: 100 emails redact, none leak, nothing crashes."""
    text = " ".join(f"write to {email()} ref{index}"
                    for index in range(100))
    started = time.perf_counter()
    decision = engine.gate(text, policy, registry, mode="send")
    elapsed = time.perf_counter() - started
    assert decision.decision == "REDACT"
    assert decision.sent_text is not None
    assert email() not in decision.sent_text
    assert len(decision.spans) == 100
    assert elapsed < TIME_BUDGET_S


def test_many_ambiguous_spans_hold_without_crash(policy, registry):
    """STOPPED: 200 high-entropy tokens HOLD as one case, no crash."""
    token = "aB3xK9qW2mZ7pL5sT8vN4yU6cE"
    text = " ".join(f"{token}{index:04d}x" for index in range(200))
    started = time.perf_counter()
    decision = engine.gate(text, policy, registry, mode="send")
    elapsed = time.perf_counter() - started
    assert decision.decision == "HOLD"
    assert decision.sent_text is None
    assert len(decision.spans) == 200
    assert elapsed < TIME_BUDGET_S


def test_long_clean_text_allows(policy, registry):
    """STOPPED: 50 KB of clean prose allows without crashing."""
    text = "lorem ipsum dolor sit amet " * 2000
    assert len(text) > 50_000
    started = time.perf_counter()
    decision = engine.gate(text, policy, registry, mode="send")
    elapsed = time.perf_counter() - started
    assert decision.decision == "ALLOW"
    assert decision.spans == []
    assert elapsed < TIME_BUDGET_S
