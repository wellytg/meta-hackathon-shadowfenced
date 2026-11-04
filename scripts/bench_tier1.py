"""Benchmark Tier 1 scan speed over every inline corpus message.

Expands all cases with inline ``input`` from ``corpus/cases.yaml``,
``corpus/holdout.yaml`` and ``corpus/final.yaml`` (107 messages; the 3
file-based cases in ``cases.yaml`` are excluded) and times
``tier1.scan`` on each message. Prints n, p50, p95 and max in
milliseconds. Percentiles use the same rank method as ``scripts/eval.py``.

Usage: ``python scripts/bench_tier1.py [--seed SEED] [--repeat N]``.
``--repeat`` scans each message N times and keeps the best time per
message (default 5: best-of-N shrugs off scheduling noise on a busy
box; real gate latency is one scan plus that same noise).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CORPORA = ("cases", "holdout", "final")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Tier 1 scans.")
    parser.add_argument("--seed", default="demo-seed")
    parser.add_argument("--repeat", type=int, default=5)
    return parser.parse_args(argv)


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    rank = min(len(ordered) - 1, int(pct / 100 * len(ordered)))
    return ordered[rank]


def collect_messages(seed: str) -> list[str]:
    """Expand every inline-input case across the three corpora."""
    from shadowfenced.corpus import expand_all, load_cases

    messages: list[str] = []
    for name in CORPORA:
        raw = load_cases(ROOT / "corpus" / f"{name}.yaml")
        inline = [case for case in raw if "input" in case]
        messages.extend(case["text"] for case in expand_all(inline, seed))
    return messages


def benchmark(messages: list[str], repeat: int) -> list[float]:
    """Best-of-``repeat`` scan time per message, in milliseconds."""
    from shadowfenced.canary import CanaryRegistry
    from shadowfenced.corpus import REGISTRY_KINDS
    from shadowfenced import tier1
    from shadowfenced.config import get_settings
    from shadowfenced.policy import load_policy

    policy = load_policy()
    registry = CanaryRegistry(get_settings().canary_seed, REGISTRY_KINDS)
    # Warm up once: pattern compilation is a one-time process cost,
    # not per-message latency, so it must not land on one message.
    tier1.scan("warm up", policy, registry)
    best: list[float] = []
    for text in messages:
        times: list[float] = []
        for _ in range(max(1, repeat)):
            started = time.perf_counter()
            tier1.scan(text, policy, registry)
            times.append((time.perf_counter() - started) * 1000)
        best.append(min(times))
    return best


def main(argv: list[str] | None = None) -> dict[str, float]:
    """Run the benchmark, print the summary, return it as a dict."""
    args = _parse_args(argv)
    messages = collect_messages(args.seed)
    times = benchmark(messages, args.repeat)
    summary = {
        "n": float(len(times)),
        "p50": round(_percentile(times, 50), 2),
        "p95": round(_percentile(times, 95), 2),
        "max": round(max(times), 2) if times else 0.0,
    }
    print(
        f"tier1 over {len(times)} corpus messages "
        f"(seed {args.seed}, repeat {max(1, args.repeat)}): "
        f"p50={summary['p50']} p95={summary['p95']} "
        f"max={summary['max']} ms"
    )
    return summary


if __name__ == "__main__":
    main()
