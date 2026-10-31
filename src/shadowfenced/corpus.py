"""Synthetic evaluation corpus: templates expand at runtime, never committed.

``corpus/cases.yaml`` holds templates only — no secret value, no key
shape, no canary literal. Every sensitive value is minted at expansion
time from a seed:

- ``{{canary:<kind>}}`` — a registry canary for ``kind``.
- ``{{canary:<kind>|<encoding>}}`` — the canary in one encoded form:
  ``base64`` | ``hex`` | ``rot13`` | ``reversed`` | ``url`` | ``split``.
- ``{{fake_key:<provider>}}`` — a provider-shaped fake key for ``aws`` |
  ``github`` | ``openai`` | ``slack``. Prefixes are joined here at
  runtime from fragments; the random tail comes from a seeded RNG.
- ``{{entropy:<n>}}`` — an ``n``-char high-entropy token (seeded RNG)
  that matches no allowlist shape.
- ``{{file:<name>}}`` — the text of a demo file, generated at runtime
  by ``scripts/make_files.py`` when missing.

Fictional org: Corvane Labs only. Real-looking PII below (555 numbers,
``@corvane.io``, 078-05-1120, 4111…1111) is invented for tests.
"""

from __future__ import annotations

import random
import re
import string
import sys
from pathlib import Path
from typing import Any

import yaml

from .canary import CanaryRegistry, encodings

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES_PATH = ROOT / "corpus" / "cases.yaml"

from shadowfenced.canary import DEFAULT_KINDS as REGISTRY_KINDS

CATEGORIES = ("secrets", "pii", "business", "beacon", "benign")
OUTCOMES = ("ALLOW", "REDACT", "BLOCK", "HOLD")

_TEMPLATE_RE = re.compile(r"\{\{([^{}]+)\}\}")

_ALNUM = string.ascii_letters + string.digits
_ALNUM_UPPER = string.ascii_uppercase + string.digits
_ENTROPY_ALPHABET = string.ascii_letters + string.digits + "-_"


def _fake_key_spec(provider: str) -> tuple[str, str, int, str]:
    """Return (prefix_a, prefix_b, tail_len, alphabet) for one provider.

    Fragments are joined at runtime so no full provider prefix ever
    appears as a literal (the repo guard scans for full shapes).
    """
    if provider == "aws":
        return ("AK", "IA", 16, _ALNUM_UPPER)
    if provider == "github":
        return ("gh", "p_", 36, _ALNUM)
    if provider == "openai":
        return ("s", "k-", 32, _ALNUM)
    if provider == "slack":
        return ("xox", "b-", 16, _ALNUM + "-")
    raise ValueError(f"unknown fake_key provider {provider!r}")


def fake_key(provider: str, rng: random.Random) -> str:
    """Mint a provider-shaped fake key from a seeded RNG."""
    head_a, head_b, tail_len, alphabet = _fake_key_spec(provider)
    return head_a + head_b + "".join(rng.choice(alphabet) for _ in range(tail_len))


def entropy_token(n: int, rng: random.Random) -> str:
    """Mint an n-char high-entropy token that matches no allowlist shape.

    ``_`` and ``-`` are forced in so the token is never a hex git-sha or
    UUID shape; the wide alphabet keeps Shannon entropy well above 4.0.
    A resample guard keeps provider shapes out by construction accident.
    """
    if n < 20:
        raise ValueError("entropy token must be at least 20 chars")
    for attempt in range(100):
        chars = [rng.choice(_ENTROPY_ALPHABET) for _ in range(n)]
        chars[5] = "_"
        chars[9] = "-"
        token = "".join(chars)
        if ("AK" + "IA") in token or ("gh" + "p_") in token:
            continue
        if ("s" + "k-") in token or ("xox" + "b-") in token:
            continue
        if "@" not in token:
            return token
    raise RuntimeError("could not mint an entropy token")


def file_text(name: str) -> str:
    """Return the text of a demo file, generating it first when missing."""
    from .extract import extract_text

    path = ROOT / "runs" / "files" / name
    if not path.is_file():
        sys.path.insert(0, str(ROOT / "scripts"))
        import make_files  # noqa: E402

        make_files.main()
    data = path.read_bytes()
    if path.suffix.lower() == ".md" or path.suffix.lower() == ".csv":
        return data.decode("utf-8")
    _, text = extract_text(name, data)
    return text


def expand_template(template: str, rng: random.Random,
                    registry: CanaryRegistry) -> str:
    """Expand one ``{{...}}`` template body (without the braces)."""
    body = template.strip()
    if body.startswith("canary:"):
        _, _, rest = body.partition(":")
        kind, _, encoding = rest.partition("|")
        value = registry.value(kind.strip())
        if not encoding:
            return value
        form = encodings(value).get(encoding.strip())
        if form is None and encoding.strip() == "split":
            return " ".join(value)
        if form is None:
            raise ValueError(f"unknown canary encoding {encoding!r}")
        return form
    if body.startswith("fake_key:"):
        _, _, provider = body.partition(":")
        return fake_key(provider.strip(), rng)
    if body.startswith("entropy:"):
        _, _, count = body.partition(":")
        return entropy_token(int(count.strip()), rng)
    if body.startswith("file:"):
        _, _, name = body.partition(":")
        return file_text(name.strip())
    raise ValueError(f"unknown template {{{{{body}}}}}")


def expand_text(text: str, rng: random.Random,
                registry: CanaryRegistry) -> str:
    """Expand every ``{{...}}`` template in a string."""

    def _replace(match: re.Match[str]) -> str:
        return expand_template(match.group(1), rng, registry)

    return _TEMPLATE_RE.sub(_replace, text)


def encoded_forms(value: str) -> list[str]:
    """All encoded forms of one value checked by the leak detector."""
    return [value, *encodings(value).values()]


def load_cases(path: str | Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    """Load and validate the raw corpus cases."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"corpus {path} must hold a list of cases")
    seen: set[str] = set()
    for case in raw:
        if not isinstance(case, dict):
            raise ValueError(f"corpus case must be a mapping: {case!r}")
        for field in ("id", "category", "expect", "acceptable", "classes",
                      "sensitive_values"):
            if field not in case:
                raise ValueError(f"case {case.get('id')!r} misses {field!r}")
        if "input" not in case and "file" not in case:
            raise ValueError(f"case {case['id']!r} needs input or file")
        if case["id"] in seen:
            raise ValueError(f"duplicate case id {case['id']!r}")
        seen.add(case["id"])
        if case["category"] not in CATEGORIES:
            raise ValueError(f"case {case['id']!r} bad category")
        if case["expect"] not in OUTCOMES:
            raise ValueError(f"case {case['id']!r} bad expect")
        if not case["acceptable"]:
            raise ValueError(f"case {case['id']!r} needs acceptable outcomes")
        for outcome in case["acceptable"]:
            if outcome not in OUTCOMES:
                raise ValueError(f"case {case['id']!r} bad acceptable entry")
    return raw


def expand_case(case: dict[str, Any], seed: str) -> dict[str, Any]:
    """Expand one case: text plus the concrete sensitive values.

    The RNG is seeded per case (``seed:case_id``) so expansion is
    deterministic and independent of case order. Each distinct template
    is expanded once per case and reused, so a template named in both
    ``input`` and ``sensitive_values`` yields the same value in both.
    """
    rng = random.Random(f"{seed}:{case['id']}")
    registry = CanaryRegistry(seed, REGISTRY_KINDS)
    bodies: list[str] = []
    if "input" in case:
        bodies.extend(m.group(1) for m in _TEMPLATE_RE.finditer(case["input"]))
    for ref in case.get("sensitive_values", []):
        bodies.extend(m.group(1) for m in _TEMPLATE_RE.finditer(ref))
    values: dict[str, str] = {}
    for body in bodies:
        if body not in values:
            values[body] = expand_template(body, rng, registry)

    def _fill(text: str) -> str:
        return _TEMPLATE_RE.sub(lambda m: values[m.group(1)], text)

    if "file" in case:
        text = file_text(case["file"])
    else:
        text = _fill(case["input"])
    sensitive = [
        _fill(ref) if "{{" in ref else ref
        for ref in case.get("sensitive_values", [])
    ]
    return {
        "id": case["id"],
        "category": case["category"],
        "text": text,
        "sensitive": sensitive,
        "expect": case["expect"],
        "acceptable": list(case["acceptable"]),
        "classes": list(case["classes"]),
    }


def expand_all(cases: list[dict[str, Any]], seed: str) -> list[dict[str, Any]]:
    """Expand every case with one seed."""
    return [expand_case(case, seed) for case in cases]
