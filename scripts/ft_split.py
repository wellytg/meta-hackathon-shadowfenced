"""Deterministic train/val/test split (80/10/10, seeded).

Reads ``runs/ft/labeled.jsonl``, shuffles with a seeded RNG, and writes
``train.jsonl`` / ``val.jsonl`` / ``test.jsonl`` plus a ``split.json``
manifest (seed, counts, per-split id lists) so the split is auditable.
The test split is written separately and is never used for training:
``ft_train.py`` only reads the train/val files.

Usage::

    python scripts/ft_split.py [--in runs/ft/labeled.jsonl]
                               [--out-dir runs/ft] [--seed 7]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TRAIN_FRAC = 0.8
VAL_FRAC = 0.1


def split_rows(rows: list[dict], seed: int = 7) -> dict[str, list[dict]]:
    """Seeded shuffle then 80/10/10 split. Pure: no I/O, deterministic."""
    order = list(rows)
    random.Random(seed).shuffle(order)
    n = len(order)
    n_train = int(n * TRAIN_FRAC)
    n_val = int(n * VAL_FRAC)
    return {
        "train": order[:n_train],
        "val": order[n_train:n_train + n_val],
        "test": order[n_train + n_val:],
    }


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="inp",
                        default=str(ROOT / "runs" / "ft" / "labeled.jsonl"))
    parser.add_argument("--out-dir", default=str(ROOT / "runs" / "ft"))
    parser.add_argument("--seed", type=int, default=7)
    return parser.parse_args(argv)


def main(argv=None) -> dict[str, list[dict]]:
    args = _parse_args(argv)
    rows = [json.loads(line) for line in
            Path(args.inp).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    parts = split_rows(rows, args.seed)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, split in parts.items():
        with (out_dir / f"{name}.jsonl").open("w", encoding="utf-8") as handle:
            for row in split:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = {
        "seed": args.seed,
        "counts": {name: len(split) for name, split in parts.items()},
        "ids": {name: [row.get("id") for row in split]
                for name, split in parts.items()},
    }
    (out_dir / "split.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print("split: " + ", ".join(
        f"{name}={len(split)}" for name, split in parts.items()))
    return parts


if __name__ == "__main__":
    main()
