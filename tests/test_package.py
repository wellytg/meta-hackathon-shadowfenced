"""Real test: the package imports and exposes its modules."""

import importlib


def test_package_imports():
    pkg = importlib.import_module("shadowfenced")
    assert pkg.__version__ == "0.0.0"
    for name in ("spans", "canary", "policy", "tier1", "verifier", "ecs_log", "app"):
        assert importlib.import_module(f"shadowfenced.{name}") is not None
