"""Thin evaluation runner: one entry point for the existing eval tools.

This script builds command lines and runs them; all evaluation logic
lives in the tools it calls (nothing is duplicated here):

- ``--suite final|holdout|cases`` runs ``scripts/eval.py`` with
  ``--corpus <suite> --judge <glimmer|llama|none>`` and prints where
  the result file was written (``runs/eval_<date>_<suite>_<judge>.json``).
- ``--bench`` runs ``scripts/bench_tier1.py`` (Tier 1 latency).
- ``--quick [--local]`` runs ``setup/try_cases.py`` (10 live cases
  against the hosted demo, or ``--local`` for 127.0.0.1:8765).

Synthetic corpora only; nothing here prints or copies any key.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SUITES = ("final", "holdout", "cases")
JUDGES = ("glimmer", "llama", "none")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Shadowfenced evaluations (thin wrapper).")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--suite", choices=SUITES,
                        help="evaluate corpus/<suite>.yaml via scripts/eval.py")
    action.add_argument("--bench", action="store_true",
                        help="benchmark Tier 1 via scripts/bench_tier1.py")
    action.add_argument("--quick", action="store_true",
                        help="try 10 live cases via setup/try_cases.py")
    parser.add_argument("--judge", choices=JUDGES, default="glimmer",
                        help="eval judge (with --suite; default glimmer)")
    parser.add_argument("--local", action="store_true",
                        help="try the local app (with --quick)")
    args = parser.parse_args(argv)
    if args.judge != "glimmer" and args.suite is None:
        parser.error("--judge needs --suite")
    if args.local and not args.quick:
        parser.error("--local needs --quick")
    return args


def build_eval_command(suite: str, judge: str) -> list[str]:
    """Command line for ``scripts/eval.py`` on one suite and judge."""
    return [sys.executable, str(ROOT / "scripts" / "eval.py"),
            "--corpus", suite, "--judge", judge]


def build_bench_command() -> list[str]:
    """Command line for ``scripts/bench_tier1.py``."""
    return [sys.executable, str(ROOT / "scripts" / "bench_tier1.py")]


def build_quick_command(local: bool) -> list[str]:
    """Command line for ``setup/try_cases.py`` (``--local`` optional)."""
    cmd = [sys.executable, str(ROOT / "setup" / "try_cases.py")]
    if local:
        cmd.append("--local")
    return cmd


def latest_result_file(suite: str, judge: str) -> Path | None:
    """Newest ``runs/eval_*_<suite>_<judge>.json``, or None when absent."""
    runs = ROOT / "runs"
    candidates = sorted(runs.glob(f"eval_*_{suite}_{judge}.json"))
    return candidates[-1] if candidates else None


def run_command(cmd: list[str]) -> int:
    """Run one command from the repo root; return its exit code."""
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    """Build the requested command, run it, report the result file."""
    args = _parse_args(argv)
    if args.suite is not None:
        cmd = build_eval_command(args.suite, args.judge)
    elif args.bench:
        cmd = build_bench_command()
    else:
        cmd = build_quick_command(args.local)
    print("Running: " + " ".join(cmd), flush=True)
    code = run_command(cmd)
    if code == 0 and args.suite is not None:
        found = latest_result_file(args.suite, args.judge)
        if found is not None:
            print(f"Result file: {found}", flush=True)
        else:
            print("Result file: see the 'wrote ...' line above (runs/).",
                  flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
