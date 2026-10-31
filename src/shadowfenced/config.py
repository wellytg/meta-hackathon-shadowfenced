"""Settings from the environment plus an optional ``.env`` file.

A tiny ``KEY=VALUE`` parser reads ``.env`` (never committed); real
environment variables always win. Values are never printed or logged
by this module.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

_ENV_KEYS = (
    "OLLAMA_URL",
    "SF_UPSTREAM",
    "SF_LOCAL_UPSTREAM_MODEL",
    "META_API_KEY",
    "MODEL_API_KEY",
    "META_BASE_URL",
    "META_MODEL",
    "SF_CANARY_SEED",
    "SF_LOG_PATH",
    "SF_LIVE_MODEL",
    "SF_JUDGE_MODEL",
    "SF_JUDGE_TIMEOUT_S",
    "SF_LIVE_TIMEOUT_S",
    "SF_LIVE_NUM_GPU",
    "SF_TIER2_MODE",
)

DEFAULT_LOG_PATH = "runs/decisions.jsonl"


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` dotenv file (no interpolation)."""
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _find_dotenv() -> Path | None:
    """Locate ``.env``: current directory first, then the repo root."""
    here = Path.cwd() / ".env"
    if here.is_file():
        return here
    root = Path(__file__).resolve().parents[2] / ".env"
    if root.is_file():
        return root
    return None


class Settings(BaseModel):
    """Runtime settings. ``SF_UPSTREAM`` resolves to ``meta`` only when
    explicitly set or when ``META_API_KEY`` is present; otherwise local
    Ollama is the default. Empty Meta fields mean Meta is unavailable."""

    ollama_url: str = _DEFAULT_OLLAMA_URL
    upstream: str = "ollama"
    local_model: str = "llama3.2:3b"
    meta_api_key: str = ""
    meta_base_url: str = "https://api.meta.ai/v1"
    meta_model: str = "muse-spark-1.3"
    canary_seed: str = "demo-seed"
    log_path: str = DEFAULT_LOG_PATH
    live_model: str = "llama3.2:3b"
    judge_model: str = "muse-glimmer:30b"
    judge_timeout_s: float = 60.0
    live_timeout_s: float = 4.0
    live_num_gpu: int = 0
    tier2_mode: str = "live"

    @property
    def meta_available(self) -> bool:
        """True when Meta upstream has everything it needs."""
        return bool(self.meta_api_key and self.meta_base_url and self.meta_model)


def _collect(env: dict[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ) if env is None else dict(env)
    dotenv_path = _find_dotenv() if env is None else None
    file_values = _parse_dotenv(dotenv_path) if dotenv_path else {}
    merged = dict(file_values)
    merged.update(source)  # real environment always wins
    return {k: v for k, v in merged.items() if k in _ENV_KEYS}


def _tier2_mode(raw: str | None) -> str:
    mode = (raw or "live").strip().lower()
    return mode if mode in ("live", "replay", "off") else "live"


def _parse_float(raw: str | None, default: float) -> float:
    try:
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _parse_int(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def get_settings(env: dict[str, str] | None = None) -> Settings:
    """Build settings from the environment (plus ``.env`` by default).

    ``env`` overrides the process environment (used by tests); ``.env``
    is only read when ``env`` is None.
    """
    merged = _collect(env)
    file_or_env_upstream = merged.get("SF_UPSTREAM", "").strip().lower()
    # MODEL_API_KEY is an accepted alias for META_API_KEY; neither value
    # is ever logged or printed by this module.
    meta_key = merged.get("META_API_KEY", "") or merged.get("MODEL_API_KEY", "")
    if file_or_env_upstream in ("meta", "ollama"):
        upstream = file_or_env_upstream
    else:
        upstream = "meta" if meta_key else "ollama"
    return Settings(
        ollama_url=merged.get("OLLAMA_URL", _DEFAULT_OLLAMA_URL),
        upstream=upstream,
        local_model=merged.get("SF_LOCAL_UPSTREAM_MODEL", "llama3.2:3b"),
        meta_api_key=meta_key,
        meta_base_url=merged.get("META_BASE_URL", "https://api.meta.ai/v1"),
        meta_model=merged.get("META_MODEL", "muse-spark-1.3"),
        canary_seed=merged.get("SF_CANARY_SEED", "demo-seed"),
        log_path=merged.get("SF_LOG_PATH", DEFAULT_LOG_PATH),
        live_model=merged.get("SF_LIVE_MODEL", "llama3.2:3b"),
        judge_model=merged.get("SF_JUDGE_MODEL", "muse-glimmer:30b"),
        judge_timeout_s=_parse_float(merged.get("SF_JUDGE_TIMEOUT_S"), 60.0),
        live_timeout_s=_parse_float(merged.get("SF_LIVE_TIMEOUT_S"), 4.0),
        live_num_gpu=_parse_int(merged.get("SF_LIVE_NUM_GPU"), 0),
        tier2_mode=_tier2_mode(merged.get("SF_TIER2_MODE")),
    )


# Backwards-friendly alias used across the codebase.
load_settings = get_settings
