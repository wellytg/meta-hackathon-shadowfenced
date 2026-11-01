"""Generate synthetic fine-tune data: org rules -> workplace messages.

Reads ``policy.yaml`` (plus ``policy.local.yaml`` when present), then asks
Muse Spark through the existing Meta upstream client
(``shadowfenced.upstream.chat_sync`` with ``provider="meta"``) to write
short, realistic workplace messages for the fictional org:

- leaks per class (secret, personal/pii, business, beacon) that use the
  policy rules (codenames, clients, figure cues, canary placeholders);
- near-misses that look risky but are safe (lowercase codename prose,
  placeholder passwords, allowlisted shapes);
- plainly safe messages (lunch plans, standup notes).

Output is JSONL at ``runs/ft/raw.jsonl`` with rows
``{id, text, intended_class, rule_ids}``. Rows are deduplicated, capped in
length, and screened for real-format provider key shapes: any candidate
holding one is dropped. Secret leaks use ``SF_CANARY_`` canaries only,
never real keys.

Usage::

    python scripts/ft_generate.py [--per-class N] [--near-miss N] [--safe N]
                                  [--seed SEED] [--out runs/ft/raw.jsonl]

Nothing here touches the GPU. The Meta call needs network; unit tests
inject a fake transport instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CANARY_PREFIX = "SF_CANARY_"
MAX_TEXT_CHARS = 500

# intended_class vocabulary. "personal" is accepted as an alias of "pii"
# (the task text says "personal"; the judge schema says "pii").
CLASSES = ("secret", "pii", "business", "beacon", "benign")
_CLASS_ALIASES = {"personal": "pii", "person": "pii"}


def normalize_class(name: str) -> str:
    """Map an intended-class label to the judge vocabulary."""
    key = (name or "").strip().lower()
    key = _CLASS_ALIASES.get(key, key)
    if key not in CLASSES:
        raise ValueError(f"unknown intended_class {name!r}")
    return key


def _key_shape_res() -> list[re.Pattern[str]]:
    """Real-format provider key shapes; fragments joined so no full
    prefix appears literally in this file."""
    cloud = "AK" + "IA" + r"[0-9A-Z]{16}(?![0-9A-Z])"
    dev = "gh" + "p_" + r"[A-Za-z0-9]{36}(?![A-Za-z0-9])"
    live = "s" + "k-" + r"[A-Za-z0-9]{20,}(?![A-Za-z0-9])"
    bot = "xox" + "b-" + r"[A-Za-z0-9-]{10,}"
    return [re.compile(p) for p in (cloud, dev, live, bot)]


def contains_real_key_shape(text: str) -> bool:
    """True when text holds a real-format provider key shape."""
    return any(rx.search(text) for rx in _key_shape_res())


def load_rule_hints(policy_path: Path) -> dict:
    """Read policy.yaml plus policy.local.yaml (when present).

    Returns ``{"org": str, "rules": [{id, kind, value, hint}]}`` where hint
    is a one-line generation hint for the model. Uses plain yaml so this
    script stays import-light; validation lives in ``shadowfenced.policy``.
    """
    import yaml

    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"policy file {policy_path} must contain a mapping")
    rules: list[dict] = []
    for name in [c.get("name", "") for c in raw.get("codenames", []) or []]:
        rules.append({
            "id": f"codename:{name}",
            "kind": "codename",
            "value": name,
            "hint": f"internal codename {name!r} in ALL-CAPS or near 'Project'",
        })
    for name in raw.get("clients", []) or []:
        rules.append({
            "id": f"client:{name}",
            "kind": "client",
            "value": name,
            "hint": f"client name {name!r}",
        })
    for cue in raw.get("figure_cues", []) or []:
        rules.append({
            "id": f"figure:{cue}",
            "kind": "figure_cue",
            "value": cue,
            "hint": f"unreleased-figure cue {cue!r} next to a number",
        })
    local = policy_path.parent / "policy.local.yaml"
    if local.is_file():
        extra = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
        items = extra.get("custom_rules", []) if isinstance(extra, dict) else []
        for item in items or []:
            if isinstance(item, dict) and item.get("value"):
                rules.append({
                    "id": f"custom:{item.get('id', item['value'])}",
                    "kind": item.get("kind", "pattern"),
                    "value": item["value"],
                    "hint": f"custom rule value {item['value']!r}",
                })
    return {"org": raw.get("org", "the organisation"), "rules": rules}


def build_tasks(rule_hints: dict, per_class: int, near_miss: int,
                safe: int, seed: str) -> list[dict]:
    """Build one generation task per desired message.

    A task names the target class plus 1-2 concrete rules to use, so every
    leak exercises the policy. Near-miss tasks name the trap to set
    (lowercase codename, placeholder credential, allowlisted shape) and the
    safe outcome to keep.
    """
    rng = random.Random(seed)
    rules = rule_hints["rules"]
    codenames = [r for r in rules if r["kind"] == "codename"]
    others = [r for r in rules if r["kind"] != "codename"]
    tasks: list[dict] = []

    def pick(pool: list[dict]) -> dict:
        return rng.choice(pool) if pool else {
            "id": "rule:none", "kind": "none", "value": "",
            "hint": "a generic internal detail",
        }

    leak_specs = (
        [("secret", "mention a stored credential with a %s token "
          "(use the literal token given, never invent key-like text)"
          % CANARY_PREFIX)] * per_class
        + [("pii", "include a person name after 'my name is' plus a contact "
            "detail for a fictional person (use example.com mail)")] * per_class
        + [("business", "use rule hint: %s" % pick(others or rules)["hint"])]
        * per_class
        + [("beacon", "reveal where a secret or personal data is stored or "
            "was moved (a path, vault, folder plus what sits there)"
            )] * per_class
    )
    for cls, brief in leak_specs:
        rule = pick(rules)
        tasks.append({"intended_class": cls, "rule_ids": [rule["id"]],
                      "kind": "leak", "brief": f"{brief}. Rule: {rule['hint']}."})
    traps = [
        "use a codename word in plain lowercase cooking/generic prose "
        "(must stay harmless, no Project context)",
        "show a placeholder credential such as password: changeme "
        "(no real secret)",
        "mention an allowlisted shape such as a git commit hash in a "
        "build note (harmless)",
    ]
    for _ in range(near_miss):
        rule = pick(codenames or rules)
        tasks.append({"intended_class": "benign", "rule_ids": [rule["id"]],
                      "kind": "near_miss",
                      "brief": "Near-miss, stays SAFE: %s." % rng.choice(traps)})
    safes = [
        "lunch plans with colleagues",
        "a standup status update with no names or numbers",
        "a note about refactoring tests",
        "weekend hiking plans",
    ]
    for _ in range(safe):
        tasks.append({"intended_class": "benign", "rule_ids": [],
                      "kind": "safe",
                      "brief": "Plainly safe small talk: %s."
                      % rng.choice(safes)})
    rng.shuffle(tasks)
    return tasks


def build_messages(task: dict, org: str) -> list[dict]:
    """System + user messages asking Spark for one short message."""
    return [
        {"role": "system",
         "content": "Write short realistic workplace chat messages. "
                    "Reply with ONLY the message text, no quotes or labels. "
                    "Keep it under 60 words. All people, firms and secrets "
                    "are fictional. Never write a real API key, token or "
                    "password: for credentials use only tokens starting "
                    "with %s. Never use real personal data."
                    % CANARY_PREFIX},
        {"role": "user",
         "content": "Org: %s. Write one %s message. %s"
                    % (org, task["intended_class"], task["brief"])},
    ]


def clean_text(text: str) -> str:
    """Strip, collapse whitespace, cap length."""
    compact = re.sub(r"\s+", " ", (text or "").strip())
    return compact[:MAX_TEXT_CHARS].strip()


def row_id(text: str, seed: str) -> str:
    """Stable id from text hash (dedupe keeps the first occurrence)."""
    digest = hashlib.sha256(f"{seed}\0{text}".encode()).hexdigest()[:12]
    return f"ft-{digest}"


def process_candidates(candidates: list[dict], seed: str) -> list[dict]:
    """Dedupe, cap length, drop key-shaped rows; assign stable ids."""
    seen: set[str] = set()
    rows: list[dict] = []
    for item in candidates:
        text = clean_text(item.get("text", ""))
        if not text or contains_real_key_shape(text):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "id": row_id(text, seed),
            "text": text,
            "intended_class": normalize_class(item["intended_class"]),
            "rule_ids": list(item.get("rule_ids", [])),
        })
    return rows


def generate(tasks: list[dict], org: str, seed: str,
             chat_fn=None) -> list[dict]:
    """Run one Spark call per task; return cleaned rows.

    ``chat_fn`` takes a message list and returns text; defaults to the
    Meta upstream client. Tests inject a fake.
    """
    if chat_fn is None:
        from shadowfenced.upstream import chat_sync

        def chat_fn(messages, _chat=chat_sync):  # type: ignore[misc]
            return _chat(messages, provider="meta")

    candidates: list[dict] = []
    for task in tasks:
        try:
            text = chat_fn(build_messages(task, org))
        except Exception:
            continue
        candidates.append({"text": text,
                           "intended_class": task["intended_class"],
                           "rule_ids": task["rule_ids"]})
    return process_candidates(candidates, seed)


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-class", type=int, default=8)
    parser.add_argument("--near-miss", type=int, default=16)
    parser.add_argument("--safe", type=int, default=16)
    parser.add_argument("--seed", default="finetune-v1")
    parser.add_argument("--policy", default=str(ROOT / "policy.yaml"))
    parser.add_argument("--out", default=str(ROOT / "runs" / "ft" / "raw.jsonl"))
    return parser.parse_args(argv)


def main(argv=None) -> list[dict]:
    args = _parse_args(argv)
    hints = load_rule_hints(Path(args.policy))
    tasks = build_tasks(hints, args.per_class, args.near_miss,
                        args.safe, args.seed)
    rows = generate(tasks, hints["org"], args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} rows (from {len(tasks)} tasks) to {out}")
    return rows


if __name__ == "__main__":
    main()
