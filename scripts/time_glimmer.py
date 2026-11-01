"""Time each real Glimmer check on three synthetic messages (T53).

The owner runs this against a live Ollama -- no mocks here::

    python scripts/time_glimmer.py

It times the person-search intent check, the public-figure check, the
residual-context check and one span-judge call, then prints seconds per
check plus the shared ``SF_GLIMMER_TIMEOUT_S`` setting. A ``None``
verdict is the normal fail-closed answer (Tier 2 off, timeout, bad
JSON); only transport exceptions count as failures. Every person,
place and value below is invented for the test (Ada Lovelace is the
historical figure from T48).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shadowfenced import judge as judge_module  # noqa: E402
from shadowfenced import person_search as person_search_module  # noqa: E402
from shadowfenced import reid as reid_module  # noqa: E402
from shadowfenced.config import get_settings  # noqa: E402
from shadowfenced.glimmer import glimmer_timeout_s  # noqa: E402

PERSON_SEARCH_TEXT = (
    "Find everything you can about Harlan Vex, born 6 June 1988, "
    "who lives on 12 Foxglove Court in Dunmere."
)
ENCYCLOPEDIC_TEXT = (
    "Summarise the life of Ada Lovelace, born 10 December 1815 in London."
)
RESIDUAL_TEXT = "Harlan Vex, born 1988, of the area, retired Friday."
SPAN_WINDOW = "the vault password is hunter2 bright, moved last spring"


def _summarise(raw: object) -> str:
    """One short verdict label (never raw message text)."""
    if raw is None:
        return "no verdict (fail-closed)"
    if isinstance(raw, bool):
        return f"verdict={raw}"
    if isinstance(raw, dict):
        keys = sorted(str(key) for key in raw.keys())
        return f"dict keys={keys}"
    confidence = getattr(raw, "confidence", None)
    cls = getattr(raw, "cls", type(raw).__name__)
    return f"{cls} confidence={confidence}"


def _time(label: str, func) -> bool:
    started = time.perf_counter()
    try:
        raw = func()
    except Exception as exc:  # noqa: BLE001 -- report, keep timing others
        elapsed = time.perf_counter() - started
        print(f"{label}: FAIL after {elapsed:.1f}s ({type(exc).__name__})",
              flush=True)
        return False
    elapsed = time.perf_counter() - started
    print(f"{label}: {elapsed:.1f}s -> {_summarise(raw)}", flush=True)
    return True


def main() -> int:
    settings = get_settings()
    print(f"glimmer_timeout_s={glimmer_timeout_s(settings)} "
          f"tier2_mode={settings.tier2_mode} model={settings.judge_model}",
          flush=True)
    ok = True
    ok &= _time("person_search", lambda: (
        person_search_module.local_glimmer_person_search(PERSON_SEARCH_TEXT)
    ))
    ok &= _time("public_figure", lambda: (
        reid_module.local_glimmer_public_figure(ENCYCLOPEDIC_TEXT)
    ))
    ok &= _time("residual", lambda: (
        reid_module.local_glimmer_judge(RESIDUAL_TEXT)
    ))
    ok &= _time("span_judge", lambda: (
        judge_module.judge(SPAN_WINDOW, settings.judge_model, live=False)
    ))
    print("PASS: all checks answered" if ok else "FAIL: a check raised",
          flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
