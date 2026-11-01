"""T46 visuals tests: prompts YAML, dry-run key hygiene, SVG fallbacks.

No test touches the network: generation is exercised only through
``scripts/make_visuals.py --dry-run``. Key values are synthetic sentinels.
"""

from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "docs" / "assets" / "visual_prompts.yaml"
BANNER = ROOT / "docs" / "assets" / "banner.svg"
WHERE = ROOT / "docs" / "assets" / "where_muse_works.svg"

SIZE_RE = re.compile(r"^\d+x\d+$")

EXPECTED_NAMES = {
    "panel-1-typing",
    "panel-2-underlines",
    "panel-3-risk-card",
    "panel-4-placeholders-leave",
    "panel-5-answer-restored",
    "panel-6-contract-clause",
    "panel-7-env-stopped",
    "panel-8-decision-log",
    "banner",
    "where-muse-works-bg",
    "social-preview",
}


def _load_prompts():
    raw = yaml.safe_load(PROMPTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and isinstance(raw.get("images"), list)
    return raw


def test_prompts_file_parses_with_expected_entries():
    raw = _load_prompts()
    names = {item["name"] for item in raw["images"]}
    assert EXPECTED_NAMES <= names


def test_every_entry_has_size_and_output_name():
    raw = _load_prompts()
    seen_outputs: set[str] = set()
    for item in raw["images"]:
        assert SIZE_RE.match(str(item.get("size") or "")), item.get("name")
        output = str(item.get("output") or "")
        assert output.endswith(".png") and len(output) > 4, item.get("name")
        assert output not in seen_outputs, output
        seen_outputs.add(output)
        assert str(item.get("prompt") or "").strip(), item.get("name")
    # The eight storyboard panels carry no narration captions.
    for item in raw["images"]:
        if str(item["name"]).startswith("panel-"):
            assert "Narration:" not in str(item["prompt"])


def _run_dry_run(extra_args: list[str], sentinel: str) -> subprocess.CompletedProcess[str]:
    import os

    env = dict(os.environ)
    env["META_API_KEY"] = sentinel
    env.pop("MODEL_API_KEY", None)
    return subprocess.run(
        [sys.executable, "scripts/make_visuals.py", "--dry-run", *extra_args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def test_dry_run_never_contains_key(tmp_path):
    sentinel = "unit-test-sentinel-t46-dryrun-abc123"
    proc = _run_dry_run([], sentinel)
    assert proc.returncode == 0, proc.stderr
    combined = proc.stdout + proc.stderr
    assert sentinel not in combined
    assert "panel-1-typing" in proc.stdout
    assert "banner" in proc.stdout


def test_dry_run_only_runs_one(monkeypatch):
    sentinel = "unit-test-sentinel-t46-only-xyz789"
    monkeypatch.setenv("META_API_KEY", sentinel)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    proc = subprocess.run(
        [sys.executable, "scripts/make_visuals.py", "--dry-run", "--only", "banner"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "banner" in proc.stdout
    assert "panel-1-typing" not in proc.stdout
    assert sentinel not in (proc.stdout + proc.stderr)


def test_svg_fallbacks_are_valid_xml():
    for path in (BANNER, WHERE):
        assert path.is_file(), path
        ET.parse(str(path))


def test_where_svg_names_muse_roles():
    text = WHERE.read_text(encoding="utf-8")
    for role in (
        "Muse Glimmer",
        "local judge",
        "Muse Spark",
        "upstream AI",
        "hosted judge",
        "Muse Voice",
        "dictation",
        "SAM 3.1",
        "image masks",
        "Muse Code",
        "built it",
    ):
        assert role in text, role
