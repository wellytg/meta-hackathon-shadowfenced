"""T49: one shared Ollama context size for every Glimmer call.

Every native Ollama request body (``/api/chat`` or ``/api/generate``)
under ``src/shadowfenced`` must build its ``options`` through
``glimmer.glimmer_options`` (no literal ``num_ctx`` anywhere else) and
send the shared ``keep_alive``, so Ollama never reloads the 17 GB model
on a context-size switch.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from shadowfenced import judge as judge_module
from shadowfenced import person_search as person_search_module
from shadowfenced import reid as reid_module
from shadowfenced.glimmer import (
    DEFAULT_GLIMMER_NUM_CTX,
    glimmer_keep_alive,
    glimmer_num_ctx,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "shadowfenced"

_JUDGE_CONTENT = json.dumps(
    {
        "cls": "benign",
        "is_beacon": False,
        "sensitivity": 0.1,
        "confidence": 0.9,
        "span": "",
    }
)


def _src_text(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def test_no_literal_num_ctx_outside_shared_helper():
    """Only glimmer.py may set the "num_ctx" options key."""
    hits = []
    for path in sorted(SRC.glob("*.py")):
        if path.name == "glimmer.py":
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if '"num_ctx"' in line or "'num_ctx'" in line:
                hits.append(f"{path.name}:{lineno}: {line.strip()}")
    assert hits == [], f"literal num_ctx outside glimmer.py: {hits}"


def test_every_native_ollama_body_uses_shared_helper():
    """Each native Ollama body takes options/keep_alive from the helper."""
    missing = []
    for path in sorted(SRC.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "/api/chat" in text or "/api/generate" in text:
            if "glimmer_options" not in text or "glimmer_keep_alive" not in text:
                missing.append(path.name)
    assert missing == [], f"native Ollama body without shared helper: {missing}"


def test_helper_defaults_match_production_shape():
    assert glimmer_num_ctx() == DEFAULT_GLIMMER_NUM_CTX == 2048
    assert glimmer_keep_alive() == -1  # resident; honours SF_KEEP_ALIVE


def _capture_all(monkeypatch, num_ctx=None):
    """Point every Glimmer call at one MockTransport; return the bodies."""
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    if num_ctx is not None:
        monkeypatch.setenv("SF_GLIMMER_NUM_CTX", str(num_ctx))
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": _JUDGE_CONTENT}}
        )

    transport = httpx.MockTransport(handler)
    judge_module.judge("some context around a span here", "m", live=False,
                       transport=transport)
    person_search_module.local_glimmer_person_search(
        "Find Dana Whitlock for me, I need her contact details.",
        transport=transport,
    )
    reid_module.local_glimmer_judge(
        "Mara Vell, born 14 March 1989, of 9 Bellflower Lane.",
        transport=transport,
    )
    judge_module.warmup(transport=transport)
    return bodies


def test_shared_num_ctx_and_keep_alive_across_calls(monkeypatch):
    bodies = _capture_all(monkeypatch)
    assert len(bodies) == 5  # judge + person-search + reid + 2 warm-up pings
    for body in bodies:
        assert body["options"]["num_ctx"] == 2048
    keep_alives = {json.dumps(body["keep_alive"], sort_keys=True) for body in bodies}
    assert len(keep_alives) == 1, f"keep_alive differs: {keep_alives}"


def test_num_ctx_env_override_reaches_every_call(monkeypatch):
    bodies = _capture_all(monkeypatch, num_ctx=4096)
    assert len(bodies) == 5
    for body in bodies:
        assert body["options"]["num_ctx"] == 4096
