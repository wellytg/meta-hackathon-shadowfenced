"""Real test: the package imports and exposes its modules."""

import importlib


def test_package_imports():
    pkg = importlib.import_module("tripwire")
    assert pkg.__version__ == "0.0.0"
    for name in (
        "gate", "detectors", "watcher", "verifier", "escalation",
        "ecs_log", "agent", "tools", "remotes", "metrics", "console",
    ):
        assert importlib.import_module(f"tripwire.{name}") is not None
