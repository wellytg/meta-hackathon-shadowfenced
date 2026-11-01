"""T59: the Docker/Modal image must find policy.yaml and the web UI.

The image installs the source tree editable (``pip install -e .``) so the
``parents[2]`` lookups resolve under ``/app``; the wheel additionally ships
``static/**`` as package data; and ``policy.load_policy`` honours
``SF_POLICY_PATH`` read at call time.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _package_data_patterns() -> list[str]:
    with open(ROOT / "pyproject.toml", "rb") as handle:
        data = tomllib.load(handle)
    package_data = data["tool"]["setuptools"]["package-data"]
    patterns: list[str] = []
    for value in package_data.values():
        if isinstance(value, list):
            patterns.extend(value)
    return patterns


def test_static_index_html_is_package_data():
    """``static/index.html`` must be covered by declared package data."""
    patterns = _package_data_patterns()
    assert patterns, "no [tool.setuptools.package-data] entries in pyproject.toml"
    assert any(
        fnmatch.fnmatch("static/index.html", pattern)
        or fnmatch.fnmatch("static/index.html", pattern.rstrip("/") + "/*")
        for pattern in patterns
    ), f"static/index.html not covered by package data: {patterns}"


def test_sf_policy_path_is_honoured(monkeypatch, tmp_path):
    """``SF_POLICY_PATH`` redirects ``load_policy()``; read at call time."""
    from shadowfenced.policy import _DEFAULT_PATH, load_policy

    src = ROOT / "policy.yaml"
    assert src.is_file(), "repo policy.yaml missing"

    override = tmp_path / "policy.yaml"
    text = src.read_text(encoding="utf-8")
    marker = "T59 Tmp Org"
    override.write_text(
        text.replace("org: Corvane Labs", f"org: {marker}", 1),
        encoding="utf-8",
    )
    assert marker in override.read_text(encoding="utf-8")

    # Isolate custom rules: a missing local file means no rules merged in.
    monkeypatch.setenv("SF_LOCAL_POLICY", str(tmp_path / "policy.local.yaml"))

    # Env override is honoured when no path is passed ...
    monkeypatch.setenv("SF_POLICY_PATH", str(override))
    assert load_policy().org == marker

    # ... read at call time, not import time: changing it changes the result.
    from shadowfenced.policy import default_policy_path

    other = tmp_path / "other.yaml"
    marker2 = "T59 Other Org"
    other.write_text(
        text.replace("org: Corvane Labs", f"org: {marker2}", 1),
        encoding="utf-8",
    )
    monkeypatch.setenv("SF_POLICY_PATH", str(other))
    assert default_policy_path() == other
    assert load_policy().org == marker2

    # An explicit path still wins over the env var.
    assert load_policy(src).org == "Corvane Labs"

    # Falling back to the repo default when the var is unset still works.
    monkeypatch.delenv("SF_POLICY_PATH")
    assert load_policy().org == "Corvane Labs"
    assert load_policy().org == load_policy(_DEFAULT_PATH).org


def test_dockerfile_uses_editable_install():
    """The image must install the source tree editable so it stays importable."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install --no-cache-dir -e ." in text
    assert "pip install --no-cache-dir ." not in text.replace(
        "pip install --no-cache-dir -e .", ""
    )
