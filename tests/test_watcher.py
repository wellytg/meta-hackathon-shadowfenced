"""Placeholder: watcher returns a validated strict-JSON verdict."""

import pytest

from tripwire import watcher


@pytest.mark.skip(reason="watcher needs a live Glimmer endpoint (scaffold only)")
def test_judge_action_returns_verdict():
    config = watcher.WatcherConfig(base_url="http://localhost:8080", model="glimmer")
    verdict = watcher.judge_action(config, "git_commit", {"message": "hi"}, [])
    assert verdict.verdict in ("suspicious", "clean")
