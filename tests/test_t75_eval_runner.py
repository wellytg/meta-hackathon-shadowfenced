"""T75 eval runner: --help works, each action builds the right command line.

``eval/run.py`` is loaded from its path (it is not a package) and its
``run_command`` layer is mocked: no test here runs a real eval, bench,
or live HTTP call. ``--help`` runs the script for real in a subprocess
(it parses args and exits before doing anything).
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_run():
    spec = importlib.util.spec_from_file_location("eval_run", ROOT / "eval" / "run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = _load_run()


def test_help_works():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "eval" / "run.py"), "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    assert "--suite" in proc.stdout
    assert "--bench" in proc.stdout
    assert "--quick" in proc.stdout
    assert "--judge" in proc.stdout


def test_suite_builds_eval_command(monkeypatch, capsys):
    seen: list[list[str]] = []
    monkeypatch.setattr(run, "run_command",
                        lambda cmd: seen.append(cmd) or 0)
    monkeypatch.setattr(run, "latest_result_file", lambda s, j: None)
    assert run.main(["--suite", "final", "--judge", "glimmer"]) == 0
    assert seen == [[sys.executable, str(ROOT / "scripts" / "eval.py"),
                     "--corpus", "final", "--judge", "glimmer"]]
    assert "scripts" in capsys.readouterr().out


def test_suite_defaults_to_glimmer_judge(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(run, "run_command",
                        lambda cmd: seen.append(cmd) or 0)
    monkeypatch.setattr(run, "latest_result_file", lambda s, j: None)
    assert run.main(["--suite", "holdout"]) == 0
    assert seen[0][-2:] == ["--judge", "glimmer"]
    assert run.main(["--suite", "cases", "--judge", "none"]) == 0
    assert seen[1][-4:] == ["--corpus", "cases", "--judge", "none"]


def test_bench_builds_bench_command(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(run, "run_command",
                        lambda cmd: seen.append(cmd) or 0)
    assert run.main(["--bench"]) == 0
    assert seen == [[sys.executable, str(ROOT / "scripts" / "bench_tier1.py")]]


def test_quick_builds_try_cases_command(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(run, "run_command",
                        lambda cmd: seen.append(cmd) or 0)
    assert run.main(["--quick"]) == 0
    assert seen == [[sys.executable, str(ROOT / "setup" / "try_cases.py")]]
    assert run.main(["--quick", "--local"]) == 0
    assert seen[1] == [sys.executable, str(ROOT / "setup" / "try_cases.py"),
                       "--local"]


def test_prints_result_file_on_suite_success(monkeypatch, tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    first = runs / "eval_2026-11-01_final_glimmer.json"
    first.write_text("{}", encoding="utf-8")
    newest = runs / "eval_2026-11-03_final_glimmer.json"
    newest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "run_command", lambda cmd: 0)
    assert run.main(["--suite", "final", "--judge", "glimmer"]) == 0
    out = capsys.readouterr().out
    assert f"Result file: {newest}" in out


def test_no_result_file_line_without_suite(monkeypatch, capsys):
    monkeypatch.setattr(run, "run_command", lambda cmd: 0)
    assert run.main(["--bench"]) == 0
    assert "Result file" not in capsys.readouterr().out


def test_exit_code_passes_through(monkeypatch):
    monkeypatch.setattr(run, "run_command", lambda cmd: 3)
    assert run.main(["--bench"]) == 3


def test_needs_exactly_one_action(capsys):
    import pytest

    with pytest.raises(SystemExit) as exc:
        run.main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        run.main(["--suite", "final", "--bench"])
    assert exc.value.code == 2
    capsys.readouterr()


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


def test_no_key_shapes_under_eval():
    patterns = _key_patterns()
    hits: list[str] = []
    for path in sorted((ROOT / "eval").rglob("*")):
        if not path.is_file():
            continue
        try:
            lines = path.read_bytes().decode("utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(lines, start=1):
            for kind, pattern in patterns:
                if pattern.search(line):
                    hits.append(f"{path.name}:{lineno} looks like {kind}")
    assert hits == [], f"key shapes under eval/: {hits}"
