"""Evaluate base 3B vs tuned 3B vs Glimmer on the held-out test split.

For every row in ``runs/ft/test.jsonl`` each judge predicts a verdict
from the message text: the base ``Llama-3.2-3B-Instruct``, the QLoRA-tuned
3B (base + ``runs/ft/adapter``), and Muse Glimmer via local Ollama (the
existing ``shadowfenced.judge``). Metrics over the test split:

- class accuracy (predicted ``cls`` vs intended class);
- beacon F1 (beacon vs rest);
- leak rate: sensitive rows (intended != benign) judged safe (benign);
- false-alarm rate: benign rows judged non-benign;
- valid-JSON rate: predictions that parse to the typed verdict shape;
- p50/p95 latency per judge.

Writes ``runs/ft/eval.json`` and prints (and writes ``runs/ft/eval.md``)
a Markdown table.

Usage::

    python scripts/ft_eval.py [--test runs/ft/test.jsonl]
                              [--adapter runs/ft/adapter]
                              [--out runs/ft/eval.json] [--judge-model ...]

Each judge is a ``predict(text) -> (raw_str, latency_s)`` callable, so
tests inject fakes; only ``main()`` builds the real model/Ollama judges
(lazy imports, needs GPU + Ollama daemon).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from ft_generate import normalize_class  # noqa: E402

Predictor = Callable[[str], tuple[str, float]]

SENSITIVE = ("secret", "pii", "business", "beacon")


def parse_verdict(raw: str) -> dict | None:
    """Parse a raw prediction into the typed verdict shape; None if invalid."""
    text = (raw or "").strip()
    if text.endswith("<|eot|>"):
        text = text[: -len("<|eot|>")].strip()
    try:
        data = json.loads(text)
    except ValueError:
        return None
    try:
        cls = normalize_class(str(data["cls"]))
        sensitivity = float(data["sensitivity"])
        confidence = float(data["confidence"])
        if not (0.0 <= sensitivity <= 1.0 and 0.0 <= confidence <= 1.0):
            return None
        return {"cls": cls, "is_beacon": bool(data["is_beacon"]),
                "sensitivity": sensitivity, "confidence": confidence,
                "span": str(data["span"])}
    except (KeyError, TypeError, ValueError):
        return None


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = min(len(ordered) - 1, int(pct / 100 * len(ordered)))
    return ordered[rank]


def evaluate(rows: list[dict], predictors: dict[str, Predictor]) -> dict:
    """Run every predictor over the test rows; return per-judge metrics."""
    per_judge: dict[str, dict] = {}
    for name, predict in predictors.items():
        preds: list[dict | None] = []
        latencies: list[float] = []
        for row in rows:
            raw, latency = predict(row["text"])
            latencies.append(float(latency))
            preds.append(parse_verdict(raw))
        n = len(rows)
        valid = [p for p in preds if p is not None]
        hits = sum(1 for row, p in zip(rows, preds)
                   if p is not None and p["cls"] == row["intended_class"])
        # Beacon F1: beacon vs rest (invalid JSON counts as wrong).
        tp = sum(1 for row, p in zip(rows, preds)
                 if row["intended_class"] == "beacon"
                 and p is not None and p["cls"] == "beacon")
        fp = sum(1 for row, p in zip(rows, preds)
                 if row["intended_class"] != "beacon"
                 and p is not None and p["cls"] == "beacon")
        fn = sum(1 for row, p in zip(rows, preds)
                 if row["intended_class"] == "beacon"
                 and (p is None or p["cls"] != "beacon"))
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        beacon_f1 = (2 * precision * recall / (precision + recall)
                     if (precision + recall) else 0.0)
        sens_rows = [r for r in rows if r["intended_class"] in SENSITIVE]
        leaks = sum(1 for row, p in zip(rows, preds)
                    if row["intended_class"] in SENSITIVE
                    and (p is None or p["cls"] == "benign"))
        benign_rows = [r for r in rows if r["intended_class"] == "benign"]
        alarms = sum(1 for row, p in zip(rows, preds)
                     if row["intended_class"] == "benign"
                     and p is not None and p["cls"] != "benign")
        per_judge[name] = {
            "n": n,
            "accuracy": round(hits / n, 4) if n else 0.0,
            "beacon_f1": round(beacon_f1, 4),
            "leak_rate": round(leaks / len(sens_rows), 4) if sens_rows else 0.0,
            "false_alarm_rate": round(alarms / len(benign_rows), 4)
            if benign_rows else 0.0,
            "valid_json_rate": round(len(valid) / n, 4) if n else 0.0,
            "latency_s": {
                "p50": round(_percentile(latencies, 50), 3),
                "p95": round(_percentile(latencies, 95), 3),
            },
        }
    return {"judges": per_judge, "n_test": len(rows)}


def markdown_table(report: dict) -> str:
    """Render the eval report as a Markdown table."""
    header = ("| judge | acc | beacon F1 | leak | false alarm | "
              "valid JSON | p50 (s) | p95 (s) |")
    sep = "|---|---|---|---|---|---|---|---|"
    lines = [header, sep]
    for name, m in report["judges"].items():
        lines.append(
            f"| {name} | {m['accuracy']:.3f} | {m['beacon_f1']:.3f} | "
            f"{m['leak_rate']:.3f} | {m['false_alarm_rate']:.3f} | "
            f"{m['valid_json_rate']:.3f} | {m['latency_s']['p50']:.3f} | "
            f"{m['latency_s']['p95']:.3f} |")
    return "\n".join(lines) + "\n"


def base_predictor(model_id: str, adapter_dir: Path | None = None) -> Predictor:
    """Transformers predictor (base or base + LoRA adapter). Lazy import."""
    from peft import PeftModel  # noqa: E402
    from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

    from ft_train import SYSTEM_PROMPT  # noqa: E402

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(
        model_id, device_map="auto", load_in_4bit=True)
    if adapter_dir is not None:
        model = PeftModel.from_pretrained(model, str(adapter_dir))

    def predict(text: str) -> tuple[str, float]:
        prompt = (f"<|system|>\n{SYSTEM_PROMPT}\n"
                  f"<|user|>\n{text}\n<|assistant|>\n")
        started = time.perf_counter()
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        out = model.generate(**inputs, max_new_tokens=128,
                             do_sample=False, temperature=None, top_p=None)
        raw = tokenizer.decode(out[0][inputs["input_ids"].shape[1]:],
                               skip_special_tokens=True)
        return raw, time.perf_counter() - started

    return predict


def glimmer_predictor(judge_model: str) -> Predictor:
    """Local-Ollama Glimmer predictor via the existing judge."""
    from shadowfenced import judge as judge_module  # noqa: E402

    def predict(text: str) -> tuple[str, float]:
        started = time.perf_counter()
        verdict = judge_module.judge(text, judge_model, live=False)
        latency = time.perf_counter() - started
        raw = (json.dumps(verdict.model_dump()) if verdict is not None
               else "JUDGE_FAILED")
        return raw, latency

    return predict


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", default=str(ROOT / "runs" / "ft" / "test.jsonl"))
    parser.add_argument("--adapter", default=str(ROOT / "runs" / "ft" / "adapter"))
    parser.add_argument("--out", default=str(ROOT / "runs" / "ft" / "eval.json"))
    parser.add_argument("--model", default="meta-llama/Llama-3.2-3B-Instruct")
    parser.add_argument("--judge-model", default="muse-glimmer:30b")
    parser.add_argument("--md", default=str(ROOT / "runs" / "ft" / "eval.md"))
    return parser.parse_args(argv)


def main(argv=None) -> dict:
    args = _parse_args(argv)
    rows = [json.loads(line) for line in
            Path(args.test).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    predictors = {
        "base-3b": base_predictor(args.model),
        "tuned-3b": base_predictor(args.model, Path(args.adapter)),
        "glimmer": glimmer_predictor(args.judge_model),
    }
    report = evaluate(rows, predictors)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    table = markdown_table(report)
    Path(args.md).write_text(table, encoding="utf-8")
    print(table)
    print(f"wrote {out}")
    return report


if __name__ == "__main__":
    main()
