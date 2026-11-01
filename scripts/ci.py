"""Local CI gate: pytest, compileall, and tracked-file secret scan.

Runs the steps in order and exits non-zero on any failure.
Prints a one-line PASS/FAIL summary.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(label: str, cmd: list[str]) -> bool:
    print(f"== {label}: {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=ROOT)
    ok = proc.returncode == 0
    print(f"-- {label}: {'ok' if ok else 'FAILED'}", flush=True)
    return ok


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


def _secret_scan() -> list[str]:
    patterns = _key_patterns()
    hits: list[str] = []
    for name in _tracked_files():
        if Path(name).name == "." + "env":
            hits.append(f"{name} is a tracked env file")
            continue
        lines = _text_lines(ROOT / name)
        if lines is None:
            continue
        for lineno, line in enumerate(lines, start=1):
            for kind, pattern in patterns:
                if pattern.search(line):
                    hits.append(f"{name}:{lineno} looks like {kind}")
    return hits


def main() -> int:
    failures: list[str] = []

    if not _run("pytest", [sys.executable, "-m", "pytest", "-q"]):
        failures.append("pytest")
    if not _run("compileall", [sys.executable, "-m", "compileall", "-q", "src", "scripts"]):
        failures.append("compileall")

    print("== secret-scan: git-tracked files", flush=True)
    try:
        hits = _secret_scan()
    except subprocess.CalledProcessError as exc:
        print(f"-- secret-scan: FAILED ({exc})", flush=True)
        failures.append("secret-scan")
        hits = []
    else:
        if hits:
            for hit in hits:
                print(f"   {hit}", flush=True)
            print("-- secret-scan: FAILED", flush=True)
            failures.append("secret-scan")
        else:
            print("-- secret-scan: ok", flush=True)

    if failures:
        print(f"FAIL: {', '.join(failures)}", flush=True)
        return 1
    print("PASS: pytest, compileall, secret-scan", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
