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
    "META_BASE_URL",
    "META_MODEL",
    "SF_CANARY_SEED",
)


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
    meta_base_url: str = ""
    meta_model: str = ""
    canary_seed: str = "demo-seed"

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


def get_settings(env: dict[str, str] | None = None) -> Settings:
    """Build settings from the environment (plus ``.env`` by default).

    ``env`` overrides the process environment (used by tests); ``.env``
    is only read when ``env`` is None.
    """
    merged = _collect(env)
    file_or_env_upstream = merged.get("SF_UPSTREAM", "").strip().lower()
    meta_key = merged.get("META_API_KEY", "")
    if file_or_env_upstream in ("meta", "ollama"):
        upstream = file_or_env_upstream
    else:
        upstream = "meta" if meta_key else "ollama"
    return Settings(
        ollama_url=merged.get("OLLAMA_URL", _DEFAULT_OLLAMA_URL),
        upstream=upstream,
        local_model=merged.get("SF_LOCAL_UPSTREAM_MODEL", "llama3.2:3b"),
        meta_api_key=meta_key,
        meta_base_url=merged.get("META_BASE_URL", ""),
        meta_model=merged.get("META_MODEL", ""),
        canary_seed=merged.get("SF_CANARY_SEED", "demo-seed"),
    )


# Backwards-friendly alias used across the codebase.
load_settings = get_settings
