"""Pytest bootstrap: put src/ on sys.path so tests import shadowfenced."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def route_methods(app) -> dict:
    """Route table that works on FastAPI 0.115 and 0.162 alike.

    Reads the generated OpenAPI schema (path -> sorted UPPERCASE methods)
    instead of ``app.routes`` internals: on newer FastAPI the routes of
    included routers (e.g. ``ext_api.router``) are stored nested, so a
    table built from ``app.routes`` misses their full paths, while the
    OpenAPI schema lists every path on both versions (cf. T21b).
    """
    return {
        path: sorted(method.upper() for method in operations)
        for path, operations in app.openapi()["paths"].items()
    }


def find_static_mount(app):
    """Return the ``static`` StaticFiles mount, searching nested routers too.

    Newer FastAPI may nest included-router routes, so walk ``app.routes``
    recursively instead of assuming a flat list.
    """
    seen: set = set()

    def visit(routes):
        for route in routes or []:
            if id(route) in seen:
                continue
            seen.add(id(route))
            if route.__class__.__name__ == "Mount" and getattr(
                route, "path", ""
            ) in ("", "/"):
                return route
            for nested in (
                getattr(route, "routes", None),
                getattr(getattr(route, "router", None), "routes", None),
            ):
                if nested:
                    found = visit(nested)
                    if found is not None:
                        return found
        return None

    for top in (getattr(app, "routes", None),
                getattr(getattr(app, "router", None), "routes", None)):
        found = visit(top)
        if found is not None:
            return found
    raise AssertionError("no static mount found on app")


@pytest.fixture(autouse=True)
def _isolate_decision_log(monkeypatch, tmp_path):
    """Keep the hash-chained decision log hermetic: every test logs temp."""
    monkeypatch.setenv("SF_LOG_PATH", str(tmp_path / "decisions.jsonl"))
    # Tier 2 stays off unless a test opts in: no test may touch the network.
    monkeypatch.setenv("SF_TIER2_MODE", "off")
    # Live underline hints stay off unless a test opts in.
    monkeypatch.setenv("SF_LIVE_HINTS", "off")
