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
    "SF_GLIMMER_NUM_CTX",
    "SF_JUDGE_TIMEOUT_S",
    "SF_LIVE_TIMEOUT_S",
    "SF_LIVE_NUM_GPU",
    "SF_TIER2_MODE",
    "SF_LIVE_HINTS",
    "SF_PROTECT_KEYS",
    "SF_HOSTED",
    "SF_HOSTED_DICTATION",
    "SHADOWFENCED_EXT_TOKEN",
    "SF_EXT_TOKEN_FILE",
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
    glimmer_num_ctx: int = 2048
    judge_timeout_s: float = 60.0
    live_timeout_s: float = 4.0
    live_num_gpu: int = 0
    tier2_mode: str = "live"
    live_hints: bool = False
    protect_keys: str = "META_API_KEY,MODEL_API_KEY"
    hosted: bool = False
    hosted_dictation: bool = False
    ext_token: str = ""
    ext_token_file: str = ""

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


def _parse_flag(raw: str | None, default: bool = False) -> bool:
    """Parse an on/off env flag (on/1/true/yes enable)."""
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("on", "1", "true", "yes")


def _parse_int(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _stored_provider_key(provider_id: str) -> str:
    """One provider key from the OS store / ``.env`` fallback ("" if none)."""
    try:
        from . import providers as providers_module

        return providers_module.get_key(provider_id)
    except Exception:
        return ""


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
    if not meta_key and env is None:
        # A key stored in the OS credential store (providers UI) counts
        # exactly like an env-provided one; never logged or printed here.
        meta_key = _stored_provider_key("meta")
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
        glimmer_num_ctx=_parse_int(merged.get("SF_GLIMMER_NUM_CTX"), 2048),
        judge_timeout_s=_parse_float(merged.get("SF_JUDGE_TIMEOUT_S"), 60.0),
        live_timeout_s=_parse_float(merged.get("SF_LIVE_TIMEOUT_S"), 4.0),
        live_num_gpu=_parse_int(merged.get("SF_LIVE_NUM_GPU"), 0),
        tier2_mode=_tier2_mode(merged.get("SF_TIER2_MODE")),
        live_hints=_parse_flag(merged.get("SF_LIVE_HINTS"), False),
        protect_keys=merged["SF_PROTECT_KEYS"] if "SF_PROTECT_KEYS" in merged else "META_API_KEY,MODEL_API_KEY",
        hosted=_parse_flag(merged.get("SF_HOSTED"), False),
        hosted_dictation=_parse_flag(merged.get("SF_HOSTED_DICTATION"), False),
        ext_token=merged.get("SHADOWFENCED_EXT_TOKEN", ""),
        ext_token_file=merged.get("SF_EXT_TOKEN_FILE", ""),
    )


# Backwards-friendly alias used across the codebase.
load_settings = get_settings


def ext_token_path(settings=None) -> "Path":
    """Where the browser-extension pairing token lives on disk.

    ``SF_EXT_TOKEN_FILE`` wins, else ``~/.shadowfenced/ext_token``.
    The ``SHADOWFENCED_EXT_TOKEN`` env var (``settings.ext_token``)
    bypasses the file entirely when set.
    """
    override = ""
    if settings is not None:
        override = (settings.ext_token_file or "").strip()
    if not override:
        import os as _os

        override = _os.environ.get("SF_EXT_TOKEN_FILE", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".shadowfenced" / "ext_token"


_MEMORY_EXT_TOKEN: str | None = None


def reset_ext_token_override() -> None:
    """Forget the in-process pairing-token override (tests only)."""
    global _MEMORY_EXT_TOKEN
    _MEMORY_EXT_TOKEN = None


def rotate_ext_token(settings=None) -> str:
    """Create a fresh pairing token, invalidating the previous one.

    The new token takes precedence immediately (in-process override)
    and is also written to the token file so it survives restarts. The
    token value itself is never logged.
    """
    import secrets as _secrets

    global _MEMORY_EXT_TOKEN
    fresh = _secrets.token_urlsafe(24)
    _MEMORY_EXT_TOKEN = fresh
    path = ext_token_path(settings)
    try:
        import os as _os

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fresh + "\n", encoding="utf-8")
        try:
            _os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return fresh


def load_or_create_ext_token(settings=None) -> str:
    """Return the pairing token, generating and persisting it on first run.

    Precedence: ``SHADOWFENCED_EXT_TOKEN`` env (or ``settings.ext_token``)
    wins and is never written to disk; otherwise the token file from
    :func:`ext_token_path` is read, and when missing a fresh
    ``secrets.token_urlsafe(24)`` token is written with owner-only
    permissions. The token value itself is never logged.
    """
    if settings is None:
        try:
            settings = get_settings()
        except Exception:
            settings = None
    if _MEMORY_EXT_TOKEN:
        return _MEMORY_EXT_TOKEN
    if settings is not None and (settings.ext_token or "").strip():
        return settings.ext_token.strip()
    path = ext_token_path(settings)
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing.split()[0]
    import os as _os
    import secrets as _secrets

    fresh = _secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fresh + "\n", encoding="utf-8")
        try:
            _os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return fresh
