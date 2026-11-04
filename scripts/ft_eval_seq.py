"""Experiment: evaluate base 3B, tuned 3B and Glimmer one at a time (12 GB GPU), on the held-out
test split AND on the sealed final set (judge only, no code tiers). Reuses ft_eval's predictors and metrics."""
import gc, json, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import yaml  # noqa: E402
import ft_eval  # noqa: E402

MODEL = sys.argv[1]
ADAPTER = Path(sys.argv[2])
OUT = Path(sys.argv[3])

test_rows = [json.loads(l) for l in (ROOT / "runs/ft/test.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
final = yaml.safe_load((ROOT / "corpus/final.yaml").read_text(encoding="utf-8"))
final_rows = []
for c in final:
    text = re.sub(r"\{\{canary:(\w+)\}\}", "SF_CANARY_DB_PASSWORD_0a1b2c3d4e5f6a7b", c["input"])
    final_rows.append({"id": c["id"], "text": text, "sensitive": c.get("category") != "benign"})


def parse_cls(raw):
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        return str(json.loads(m.group(0)).get("cls", "")).lower() or None
    except Exception:
        return None


def final_metrics(pred):
    caught = flagged_benign = invalid = 0
    lat = []
    for r in final_rows:
        raw, s = pred(r["text"])
        lat.append(s)
        cls = parse_cls(raw)
        if cls is None:
            invalid += 1
        flag = cls not in (None, "safe")
        if r["sensitive"] and flag:
            caught += 1
        if not r["sensitive"] and flag:
            flagged_benign += 1
    lat.sort()
    n_s = sum(r["sensitive"] for r in final_rows)
    return {"caught": f"{caught}/{n_s}", "benign_flagged": f"{flagged_benign}/{len(final_rows) - n_s}",
            "invalid_json": invalid, "latency_p50_s": round(lat[len(lat) // 2], 3)}


report = {}
for name, make in (("base-3b", lambda: ft_eval.base_predictor(MODEL)),
                   ("tuned-3b", lambda: ft_eval.base_predictor(MODEL, ADAPTER)),
                   ("glimmer-30b", lambda: ft_eval.glimmer_predictor("muse-glimmer:30b"))):
    t = time.time()
    pred = make()
    split = ft_eval.evaluate(test_rows, {name: pred})
    report[name] = {"test_split": split, "final_set_judge_only": final_metrics(pred), "wall_s": round(time.time() - t, 1)}
    print(name, json.dumps(report[name]["final_set_judge_only"]), flush=True)
    del pred
    gc.collect()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass
OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
print("wrote", OUT)
