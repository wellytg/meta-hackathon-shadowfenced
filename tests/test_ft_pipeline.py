"""T26 tests: fine-tune pipeline (generate/label/split/train/eval).

No network, no GPU: Spark calls use a fake ``chat_fn``, Glimmer uses a
fake judge, train/eval exercise only the pure helpers. Key-shaped probe
strings are assembled from fragments at runtime, so no real-format
provider key shape ever appears literally in this file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ft_eval as fe  # noqa: E402
import ft_generate as fg  # noqa: E402
import ft_label as fl  # noqa: E402
import ft_split as fs  # noqa: E402
import ft_train as ft  # noqa: E402


def _frag(*parts: str) -> str:
    return "".join(parts)


def _key_probes() -> list[str]:
    """Runtime-built real-format key shapes (never literals in this file)."""
    return [
        _frag("AK", "IA") + "A" * 16,
        _frag("gh", "p_") + "b" * 36,
        _frag("s", "k-") + "c" * 24,
        _frag("xox", "b-") + "d" * 12,
    ]


def _canary() -> str:
    return _frag("SF", "_CANARY_") + "API_KEY_" + "0" * 16


# --- class parsing --------------------------------------------------------


def test_normalize_class_alias_and_reject():
    assert fg.normalize_class("personal") == "pii"
    assert fg.normalize_class(" Secret ") == "secret"
    assert fg.normalize_class("benign") == "benign"
    with pytest.raises(ValueError):
        fg.normalize_class("mystery")


# --- generation tasks -------------------------------------------------------


def test_build_tasks_counts_and_classes():
    hints = {"org": "Corvane Labs",
             "rules": [{"id": "codename:X", "kind": "codename",
                        "value": "X", "hint": "codename X"}]}
    tasks = fg.build_tasks(hints, per_class=2, near_miss=3, safe=4, seed="s")
    assert len(tasks) == 2 * 4 + 3 + 4
    leaks = [t for t in tasks if t["kind"] == "leak"]
    assert sorted(t["intended_class"] for t in leaks) == [
        "beacon", "beacon", "business", "business",
        "pii", "pii", "secret", "secret"]
    assert all(t["intended_class"] == "benign"
               for t in tasks if t["kind"] != "leak")


def test_build_tasks_deterministic():
    hints = {"org": "O", "rules": []}
    first = fg.build_tasks(hints, 2, 2, 2, seed="same")
    second = fg.build_tasks(hints, 2, 2, 2, seed="same")
    assert first == second


def test_build_messages_names_org_without_key_shapes():
    messages = fg.build_messages(
        {"intended_class": "secret", "rule_ids": [], "brief": "x"}, "Org")
    blob = json.dumps(messages)
    assert "Org" in blob
    assert not fg.contains_real_key_shape(blob)


# --- hygiene ----------------------------------------------------------------


def test_key_shapes_detected_canary_and_safe_clean():
    for probe in _key_probes():
        assert fg.contains_real_key_shape(f"leak {probe} here")
    assert not fg.contains_real_key_shape("hello team, lunch at noon?")
    assert not fg.contains_real_key_shape(f"token {_canary()} rotated")


def test_process_candidates_dedupes_caps_and_drops_keys():
    long_text = "word " * 300
    rows = fg.process_candidates([
        {"text": "rotate token ok", "intended_class": "secret",
         "rule_ids": ["codename:X"]},
        {"text": "Rotate Token OK", "intended_class": "secret",
         "rule_ids": []},
        {"text": f"real {_key_probes()[0]} leak",
         "intended_class": "secret", "rule_ids": []},
        {"text": "   ", "intended_class": "safe", "rule_ids": []},
        {"text": long_text, "intended_class": "personal", "rule_ids": []},
    ], seed="s")
    assert len(rows) == 2
    assert rows[0]["intended_class"] == "secret"
    assert rows[1]["intended_class"] == "pii"  # alias normalised
    assert len(rows[1]["text"]) <= fg.MAX_TEXT_CHARS
    assert rows[0]["id"].startswith("ft-")


def test_generate_uses_fake_chat_and_skips_failures():
    def fake(messages):
        body = messages[-1]["content"]
        if "nope" in body:
            raise RuntimeError("upstream down")
        return f"message about {body[:10]}"

    tasks = [
        {"intended_class": "benign", "rule_ids": [], "kind": "safe",
         "brief": "hello"},
        {"intended_class": "benign", "rule_ids": [], "kind": "safe",
         "brief": "nope"},
    ]
    rows = fg.generate(tasks, "Org", seed="s", chat_fn=fake)
    assert len(rows) == 1
    assert set(rows[0]) == {"id", "text", "intended_class", "rule_ids"}


# --- labelling --------------------------------------------------------------


def _verdict(cls="secret"):
    return {"cls": cls, "is_beacon": cls == "beacon",
            "sensitivity": 0.9, "confidence": 0.8, "span": "some span"}


def test_label_agree_and_dispute_with_fake_judge():
    rows = [
        {"id": "a", "text": "leak one", "intended_class": "secret",
         "rule_ids": []},
        {"id": "b", "text": "leak two", "intended_class": "beacon",
         "rule_ids": []},
        {"id": "c", "text": "quiet", "intended_class": "benign",
         "rule_ids": []},
    ]
    judge_fn = lambda text: (  # noqa: E731
        None if text == "quiet" else _verdict("secret"))
    labeled, disputed = fl.label_rows(
        rows, judge_fn, tier1_fn=lambda text: ["secret:canary:proven"])
    assert [r["id"] for r in labeled] == ["a"]
    assert labeled[0]["verdict"]["cls"] == "secret"
    assert labeled[0]["tier1_classes"] == ["secret:canary:proven"]
    by_id = {r["id"]: r for r in disputed}
    assert by_id["b"]["dispute_reason"].startswith("intended=beacon")
    assert by_id["c"]["dispute_reason"] == "judge_failed"


def test_label_rejects_invalid_verdict():
    rows = [{"id": "a", "text": "x", "intended_class": "secret",
             "rule_ids": []}]
    labeled, disputed = fl.label_rows(
        rows, lambda text: {"cls": "nonsense"}, tier1_fn=None)
    assert labeled == []
    assert disputed[0]["dispute_reason"] == "judge_invalid"


def test_verdict_to_dict_from_judgement_model():
    from shadowfenced.judge import Judgement

    typed = fl.verdict_to_dict(Judgement(
        cls="beacon", is_beacon=True, sensitivity=0.7,
        confidence=0.6, span="vault path"))
    assert typed == {"cls": "beacon", "is_beacon": True,
                     "sensitivity": 0.7, "confidence": 0.6,
                     "span": "vault path"}


# --- splitting --------------------------------------------------------------


def _rows(n):
    classes = ["secret", "pii", "business", "beacon", "benign"]
    return [{"id": f"ft-{i:04d}", "text": f"message {i}",
             "intended_class": classes[i % 5], "rule_ids": []}
            for i in range(n)]


def test_split_deterministic_counts_disjoint():
    first = fs.split_rows(_rows(100), seed=7)
    second = fs.split_rows(_rows(100), seed=7)
    assert first == second
    assert {len(v) for v in first.values()} == {80, 10, 10}
    ids = {name: {r["id"] for r in split}
           for name, split in first.items()}
    assert ids["train"] & ids["val"] == set()
    assert ids["train"] & ids["test"] == set()
    assert ids["val"] & ids["test"] == set()
    assert (ids["train"] | ids["val"] | ids["test"]
            == {f"ft-{i:04d}" for i in range(100)})
    other = fs.split_rows(_rows(100), seed=8)
    assert other["test"] != first["test"]


def test_split_test_never_in_train_files(tmp_path):
    src = tmp_path / "labeled.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in _rows(20)),
                   encoding="utf-8")
    parts = fs.main(["--in", str(src), "--out-dir", str(tmp_path),
                     "--seed", "7"])
    train_ids = {r["id"] for r in parts["train"]}
    reread = [json.loads(line) for line in
              (tmp_path / "test.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in reread] == [r["id"] for r in parts["test"]]
    assert train_ids & {r["id"] for r in reread} == set()
    manifest = json.loads((tmp_path / "split.json").read_text())
    assert manifest["seed"] == 7
    assert manifest["counts"] == {"train": 16, "val": 2, "test": 2}


# --- training helpers (no torch) --------------------------------------------


def test_train_chat_format_round_trips_verdict():
    verdict = _verdict("pii")
    full = ft.build_chat_text("hello", verdict)
    assert full.startswith("<|system|>")
    assert json.loads(full.split("<|assistant|>\n")[1]) == verdict
    assert "<|assistant|>" in ft.build_chat_text("hello", None)


def test_train_config_sized_for_12gb():
    cfg = ft.TrainConfig()
    assert (cfg.per_device_batch, cfg.grad_accum, cfg.max_len) == (1, 16, 1024)
    assert ft.lora_config_dict(cfg)["r"] == 16
    assert ft.quant_config_dict()["bnb_4bit_quant_type"] == "nf4"


def test_load_examples_reads_only_train_val(tmp_path):
    verdict = _verdict()
    train = tmp_path / "train.jsonl"
    val = tmp_path / "val.jsonl"
    test = tmp_path / "test.jsonl"
    train.write_text(json.dumps({"text": "t", "verdict": verdict}),
                     encoding="utf-8")
    val.write_text(json.dumps({"text": "v", "verdict": verdict}),
                   encoding="utf-8")
    test.write_text(json.dumps({"text": "SECRET-TEST", "verdict": verdict}),
                    encoding="utf-8")
    train_texts, val_texts = ft.load_examples(train, val, max_len=1024)
    assert len(train_texts) == len(val_texts) == 1
    assert "SECRET-TEST" not in json.dumps([train_texts, val_texts])


# --- eval metrics -------------------------------------------------------------


def test_parse_verdict_accepts_and_rejects():
    good = json.dumps(_verdict("beacon"))
    assert fe.parse_verdict(good + "<|eot|>")["cls"] == "beacon"
    assert fe.parse_verdict("not json") is None
    bad_range = json.dumps(_verdict() | {"confidence": 9.0})
    assert fe.parse_verdict(bad_range) is None
    assert fe.parse_verdict(json.dumps({"cls": "secret"})) is None


def test_evaluate_metrics_with_fake_predictors():
    rows = [
        {"id": "s1", "text": "leak", "intended_class": "secret"},
        {"id": "b1", "text": "beacon here", "intended_class": "beacon"},
        {"id": "ok", "text": "lunch?", "intended_class": "benign"},
    ]

    def perfect(text):
        mapping = {"leak": "secret", "beacon here": "beacon",
                   "lunch?": "benign"}
        return json.dumps(_verdict(mapping[text])), 0.5

    def sloppy(text):
        if text == "leak":
            return "oops", 2.0  # invalid JSON on a sensitive row = leak
        return json.dumps(_verdict("secret")), 1.0  # false alarm otherwise

    report = fe.evaluate(rows, {"perfect": perfect, "sloppy": sloppy})
    good = report["judges"]["perfect"]
    assert good["accuracy"] == 1.0
    assert good["beacon_f1"] == 1.0
    assert good["leak_rate"] == 0.0
    assert good["false_alarm_rate"] == 0.0
    assert good["valid_json_rate"] == 1.0
    assert good["latency_s"] == {"p50": 0.5, "p95": 0.5}
    bad = report["judges"]["sloppy"]
    assert bad["valid_json_rate"] == round(2 / 3, 4)
    assert bad["leak_rate"] == 0.5
    assert bad["false_alarm_rate"] == 1.0
    table = fe.markdown_table(report)
    assert table.startswith("| judge |")
    assert "| perfect |" in table and "| sloppy |" in table
