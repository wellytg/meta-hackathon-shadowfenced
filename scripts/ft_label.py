"""Label raw fine-tune rows with the Tier 1 + Glimmer judge.

For every row in ``runs/ft/raw.jsonl`` this runs the existing Tier 1 code
scan (for the record) and the existing local-Ollama judge
(``shadowfenced.judge.judge`` with the whole message as the window, i.e.
Muse Glimmer on the local machine). Rows where the judge verdict agrees
with ``intended_class`` go to ``runs/ft/labeled.jsonl`` with the typed
verdict ``{cls, is_beacon, sensitivity, confidence, span}``; rows where
the judge returns nothing or a different class go to
``runs/ft/disputed.jsonl`` with a ``dispute_reason`` for human review.

Usage::

    python scripts/ft_label.py [--in runs/ft/raw.jsonl]
                               [--out runs/ft/labeled.jsonl]
                               [--disputed runs/ft/disputed.jsonl]
                               [--judge-model muse-glimmer:30b]

No network beyond the local Ollama daemon. Tests inject a fake judge.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from ft_generate import normalize_class  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def verdict_to_dict(verdict: Any) -> dict:
    """Serialise a Judgement (or dict) to the typed verdict shape."""
    if isinstance(verdict, dict):
        data = dict(verdict)
    elif hasattr(verdict, "model_dump"):
        data = verdict.model_dump()
    else:
        data = {name: getattr(verdict, name) for name in
                ("cls", "is_beacon", "sensitivity", "confidence", "span")}
    get = data.get if isinstance(data, dict) else (lambda k: data[k])
    return {
        "cls": normalize_class(str(get("cls"))),
        "is_beacon": bool(get("is_beacon")),
        "sensitivity": float(get("sensitivity")),  # type: ignore[arg-type]
        "confidence": float(get("confidence")),  # type: ignore[arg-type]
        "span": str(get("span")),
    }


def agrees(intended: str, verdict: dict) -> bool:
    """True when the judge class matches the intended class exactly."""
    return verdict["cls"] == normalize_class(intended)


def label_row(row: dict, judge_fn, tier1_fn=None) -> dict:
    """Judge one raw row; return it with ``verdict`` or ``dispute_reason``.

    ``judge_fn`` takes message text and returns a Judgement/dict/None.
    ``tier1_fn`` takes text and returns span summaries (record only).
    """
    text = row.get("text", "")
    tier1_classes: list[str] = []
    if tier1_fn is not None:
        try:
            tier1_classes = list(tier1_fn(text))
        except Exception:
            tier1_classes = []
    try:
        verdict = judge_fn(text)
    except Exception:
        verdict = None
    labeled = dict(row)
    labeled["tier1_classes"] = tier1_classes
    if verdict is None:
        labeled["dispute_reason"] = "judge_failed"
        return labeled
    try:
        typed = verdict_to_dict(verdict)
    except (ValueError, TypeError, KeyError):
        labeled["dispute_reason"] = "judge_invalid"
        return labeled
    labeled["verdict"] = typed
    if not agrees(row.get("intended_class", "benign"), typed):
        labeled["dispute_reason"] = (
            f"intended={row.get('intended_class')} judged={typed['cls']}"
        )
    return labeled


def default_judge_fn(model: str):
    """Whole-message Glimmer judgement via the existing local judge."""
    from shadowfenced import judge as judge_module

    def run(text: str):
        return judge_module.judge(text, model, live=False)

    return run


def default_tier1_fn(policy, registry):
    """Tier 1 scan returning span-class summaries for the record."""
    from shadowfenced import tier1 as tier1_module

    def run(text: str):
        return [f"{s.cls}:{s.subclass}:{s.status}"
                for s in tier1_module.scan(text, policy, registry)]

    return run


def label_rows(rows: list[dict], judge_fn, tier1_fn=None) -> tuple[list, list]:
    """Split rows into (labeled, disputed)."""
    labeled, disputed = [], []
    for row in rows:
        done = label_row(row, judge_fn, tier1_fn)
        if "dispute_reason" in done:
            disputed.append(done)
        else:
            disputed_free = dict(done)
            disputed_free.pop("dispute_reason", None)
            labeled.append(disputed_free)
    return labeled, disputed


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="inp",
                        default=str(ROOT / "runs" / "ft" / "raw.jsonl"))
    parser.add_argument("--out", default=str(ROOT / "runs" / "ft" / "labeled.jsonl"))
    parser.add_argument("--disputed",
                        default=str(ROOT / "runs" / "ft" / "disputed.jsonl"))
    parser.add_argument("--judge-model", default="muse-glimmer:30b")
    parser.add_argument("--policy", default=str(ROOT / "policy.yaml"))
    parser.add_argument("--seed", default="demo-seed")
    return parser.parse_args(argv)


def main(argv=None) -> tuple[list, list]:
    args = _parse_args(argv)
    rows = read_jsonl(Path(args.inp))
    from shadowfenced.canary import DEFAULT_KINDS, CanaryRegistry
    from shadowfenced.policy import load_policy

    policy = load_policy(args.policy)
    registry = CanaryRegistry(args.seed, DEFAULT_KINDS)
    judge_fn = default_judge_fn(args.judge_model)
    tier1_fn = default_tier1_fn(policy, registry)
    labeled, disputed = label_rows(rows, judge_fn, tier1_fn)
    write_jsonl(Path(args.out), labeled)
    write_jsonl(Path(args.disputed), disputed)
    print(f"labeled {len(labeled)}, disputed {len(disputed)} "
          f"(of {len(rows)})")
    return labeled, disputed


if __name__ == "__main__":
    main()
