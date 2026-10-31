"""Offline evaluation: gate every corpus case, review HOLDs, measure leaks.

Runs every case in process through ``engine.gate`` in send mode; HOLD
cases are reviewed synchronously via ``holds.run_review``. Upstream is a
dry capture (no network): the sender records the exact outbound payload
and returns a canned answer, so ``leaked`` measures what *would* have
crossed the boundary.

Usage: ``python scripts/eval.py --judge glimmer|llama|none [--limit N]
[--seed SEED]``. ``glimmer`` judges with muse-glimmer:30b, ``llama``
with llama3.2:3b, ``none`` pins ``SF_TIER2_MODE=off`` (every HOLD stays
needs_human). Eval runs set ``SF_KEEP_ALIVE=5m`` (not -1) so repeated
judge calls do not pin GPU memory; nothing is ever unloaded explicitly.

Writes ``runs/eval_<YYYY-MM-DD>_<judge>.json`` and prints a table.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

JUDGE_MODELS = {"glimmer": "muse-glimmer:30b", "llama": "llama3.2:3b"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate shadowfenced.")
    parser.add_argument("--judge", choices=("glimmer", "llama", "none"),
                        required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--corpus", default="cases", help="corpus name under corpus/ (cases | holdout)")
    parser.add_argument("--seed", default=None)
    return parser.parse_args(argv)


def _git_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, check=True, timeout=10,
        )
        return proc.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = min(len(ordered) - 1, int(pct / 100 * len(ordered)))
    return ordered[rank]


def _latency_summary(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": round(_percentile(values, 50), 2),
        "p95": round(_percentile(values, 95), 2),
        "max": round(max(values), 2) if values else 0.0,
    }


def _leaked(outbound: str | None, sensitive: list[str]) -> bool:
    """True when any sensitive value (in any encoded form) was sent."""
    if outbound is None or not sensitive:
        return False
    from shadowfenced.corpus import encoded_forms

    for value in sensitive:
        for form in encoded_forms(value):
            if form and form in outbound:
                return True
    return False


def evaluate_cases(
    cases: list[dict[str, Any]],
    *,
    seed: str,
    policy: Any,
    registry: Any,
    judge_transport: Any = None,
    answer: str = "ok, noted.",
) -> list[dict[str, Any]]:
    """Gate every expanded case; review HOLDs synchronously; dry capture."""
    import time as _time

    from shadowfenced import engine, holds
    from shadowfenced.corpus import expand_case

    rows: list[dict[str, Any]] = []
    for raw in cases:
        case = expand_case(raw, seed)
        text = case["text"]
        gate_started = _time.perf_counter()
        decision = engine.gate(text, policy, registry, mode="send")
        gate_ms = (_time.perf_counter() - gate_started) * 1000
        review_ms = 0.0
        gate_outcome = decision.decision
        outbound: str | None = None
        final = gate_outcome
        tier = decision.resolved_tier
        if gate_outcome == "ALLOW":
            outbound = decision.sent_text
        elif gate_outcome == "REDACT":
            outbound = decision.sent_text
        elif gate_outcome == "HOLD":
            captured: dict[str, str | None] = {"payload": None}

            def _sender(sent_text: str, _cap: dict = captured) -> str:
                _cap["payload"] = sent_text
                return answer

            holds.HOLDS.clear()
            hold = holds.create_hold(
                decision, text,
                [{"role": "user", "content": text}], "ollama",
            )
            review_started = _time.perf_counter()
            holds.run_review(
                hold.hold_id, judge_transport=judge_transport,
                sender=_sender, policy=policy, registry=registry,
            )
            review_ms = (_time.perf_counter() - review_started) * 1000
            if hold.status == "released":
                outbound = hold.sent_text
                final = "REDACT" if outbound != text else "ALLOW"
                tier = 3
            elif hold.status == "redacted_sent":
                outbound = hold.sent_text
                final = "REDACT"
                tier = 3
            else:  # needs_human (or reviewing when the judge never ran)
                final = "HOLD"
                tier = 4
            holds.HOLDS.clear()
        # BLOCK and needs_human HOLD send nothing: not leaked by design.
        rows.append({
            "id": case["id"],
            "category": case["category"],
            "expect": case["expect"],
            "acceptable": case["acceptable"],
            "gate": gate_outcome,
            "final": final,
            "tier": tier,
            "meets_expect": final in case["acceptable"],
            "leaked": _leaked(outbound, case["sensitive"]),
            "gate_ms": round(gate_ms, 2),
            "review_ms": round(review_ms, 2),
            "outbound_chars": len(outbound) if outbound is not None else 0,
        })
    return rows


def compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-case rows into the eval metric set."""
    total = len(rows)
    sensitive = [r for r in rows if r["category"] != "benign"]
    benign = [r for r in rows if r["category"] == "benign"]
    initial_holds = [r for r in rows if r["gate"] == "HOLD"]
    humans = [r for r in rows if r["final"] == "HOLD"]

    def _share(part: int, whole: int) -> float:
        return round(part / whole, 4) if whole else 0.0

    tier_counts = {"1": 0, "3": 0, "4": 0}
    for row in rows:
        tier_counts[str(row["tier"])] = tier_counts.get(str(row["tier"]), 0) + 1
    tier_lat = {"1": [], "3": [], "4": []}
    for row in rows:
        key = str(row["tier"])
        if key == "1":
            tier_lat[key].append(row["gate_ms"])
        else:
            tier_lat[key].append(row["review_ms"])
    return {
        "n": total,
        "n_sensitive": len(sensitive),
        "n_benign": len(benign),
        "leak_rate": _share(
            sum(1 for r in sensitive if r["leaked"]), len(sensitive)),
        "catch_rate": _share(
            sum(1 for r in sensitive if r["final"] != "ALLOW"), len(sensitive)),
        "false_escalation_rate": _share(
            sum(1 for r in benign if r["final"] != "ALLOW"), len(benign)),
        "hold_rate": _share(len(initial_holds), total),
        "human_rate": _share(len(humans), total),
        "resolved_by_tier": {
            key: _share(count, total) for key, count in tier_counts.items()
        },
        "latency_ms": {
            key: _latency_summary(values)
            for key, values in tier_lat.items()
        },
    }


def print_table(rows: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    """Print one compact line per case plus the metric summary."""
    print(f"{'id':<18}{'cat':<9}{'gate>final':<14}{'tier':<6}{'leak':<6}ms")
    for row in rows:
        ms = row["gate_ms"] + row["review_ms"]
        print(f"{row['id']:<18}{row['category']:<9}"
              f"{row['gate'] + '>' + row['final']:<14}{row['tier']:<6}"
              f"{'YES' if row['leaked'] else '-':<6}{ms:.1f}")
    print(f"n={metrics['n']} leak_rate={metrics['leak_rate']} "
          f"catch_rate={metrics['catch_rate']} "
          f"false_esc={metrics['false_escalation_rate']} "
          f"hold={metrics['hold_rate']} human={metrics['human_rate']}")
    print(f"resolved_by_tier={metrics['resolved_by_tier']}")
    for tier, lat in metrics["latency_ms"].items():
        print(f"tier{tier}: n={lat['n']} p50={lat['p50']} "
              f"p95={lat['p95']} max={lat['max']} ms")


def build_report(rows: list[dict[str, Any]], metrics: dict[str, Any],
                 config: dict[str, Any]) -> dict[str, Any]:
    """Assemble the eval report dict written to runs/."""
    return {"config": config, "metrics": metrics, "per_case": rows}


def main(argv: list[str] | None = None) -> dict[str, Any]:
    """Run the eval; return the report dict (also written to runs/)."""
    args = _parse_args(argv)
    seed = args.seed or os.environ.get("SF_CANARY_SEED", "demo-seed")
    os.environ["SF_CANARY_SEED"] = seed
    # 5m (not -1) so eval judge calls do not pin GPU memory; live Ollama
    # models are never unloaded explicitly.
    os.environ["SF_KEEP_ALIVE"] = "5m"
    if args.judge == "none":
        os.environ["SF_TIER2_MODE"] = "off"
        judge_model = None
    else:
        os.environ["SF_TIER2_MODE"] = "live"
        os.environ["SF_JUDGE_MODEL"] = JUDGE_MODELS[args.judge]
        judge_model = JUDGE_MODELS[args.judge]
    os.environ.setdefault("SF_LOG_PATH", f"runs/eval_{args.judge}.jsonl")

    from shadowfenced.canary import CanaryRegistry
    from shadowfenced.config import get_settings
    from shadowfenced.corpus import REGISTRY_KINDS, load_cases
    from shadowfenced.policy import load_policy

    settings = get_settings()
    policy = load_policy()
    registry = CanaryRegistry(settings.canary_seed, REGISTRY_KINDS)
    raw = load_cases(ROOT / "corpus" / f"{args.corpus}.yaml")
    if args.limit:
        raw = raw[:args.limit]
    gate_started = time.perf_counter()
    rows = evaluate_cases(raw, seed=seed, policy=policy, registry=registry)
    wall_s = round(time.perf_counter() - gate_started, 1)
    metrics = compute_metrics(rows)
    report = build_report(
        rows,
        metrics,
        {
            "judge": args.judge,
            "corpus": args.corpus,
            "judge_model": judge_model,
            "seed": seed,
            "git_sha": _git_sha(),
            "date": datetime.now().isoformat(timespec="seconds"),
            "keep_alive": "5m",
            "wall_s": wall_s,
        },
    )
    out = ROOT / "runs" / f"eval_{date.today().isoformat()}_{args.corpus}_{args.judge}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_table(rows, metrics)
    print(f"wrote {out}")
    return report


if __name__ == "__main__":
    main()
