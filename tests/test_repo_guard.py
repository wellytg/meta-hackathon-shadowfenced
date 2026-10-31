"""Repo guard: no real provider key shapes and no .env among tracked files.

Key-shape prefixes are assembled at runtime from fragments (never written
as one literal) so this file can never match its own patterns.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tracked_files() -> list[str]:
    proc = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT}", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [entry for entry in proc.stdout.split("\0") if entry]


def _key_patterns() -> list[tuple[str, re.Pattern[str]]]:
    # Fragments are joined at runtime; no full prefix appears literally here.
    cloud_key = "AK" + "IA" + r"[0-9A-Z]{16}(?![0-9A-Z])"
    dev_token = "gh" + "p_" + r"[A-Za-z0-9]{36}(?![A-Za-z0-9])"
    live_secret = "s" + "k-" + r"[A-Za-z0-9]{20,}(?![A-Za-z0-9])"
    bot_token = "xox" + "b-" + r"[A-Za-z0-9-]{10,}"
    return [
        ("cloud_key", re.compile(cloud_key)),
        ("dev_token", re.compile(dev_token)),
        ("live_secret", re.compile(live_secret)),
        ("bot_token", re.compile(bot_token)),
    ]


def _text_lines(path: Path) -> list[str] | None:
    try:
        return path.read_bytes().decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def test_no_provider_key_shapes_in_tracked_files():
    patterns = _key_patterns()
    hits: list[str] = []
    for name in _tracked_files():
        lines = _text_lines(ROOT / name)
        if lines is None:
            continue
        for lineno, line in enumerate(lines, start=1):
            for kind, pattern in patterns:
                if pattern.search(line):
                    hits.append(f"{name}:{lineno} looks like {kind}")
    assert hits == [], f"provider key shapes in tracked files: {hits}"


def test_no_env_file_tracked():
    tracked = _tracked_files()
    env_files = [name for name in tracked if Path(name).name == ".env"]
    assert env_files == [], f".env file tracked: {env_files}"
