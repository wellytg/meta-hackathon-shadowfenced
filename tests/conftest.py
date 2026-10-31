"""Pytest bootstrap: put src/ on sys.path so tests import shadowfenced."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture(autouse=True)
def _isolate_decision_log(monkeypatch, tmp_path):
    """Keep the hash-chained decision log hermetic: every test logs temp."""
    monkeypatch.setenv("SF_LOG_PATH", str(tmp_path / "decisions.jsonl"))
    # Tier 2 stays off unless a test opts in: no test may touch the network.
    monkeypatch.setenv("SF_TIER2_MODE", "off")
