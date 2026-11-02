"""Live component status (T40): cheap local checks, never a paid API.

:func:`collect_components` powers ``GET /api/health/components``. Every
check is local-only:

- Tier 1 runs a self-test scan on a built-in canary minted at runtime.
- The local judge check only asks Ollama for ``/api/version`` and
  ``/api/tags`` on loopback (2s timeout); it never sends prompts.
- The Meta and Voice checks only report whether a key is configured;
  key values are never read here, never returned, never logged.
- Keystore, log chain, extension pairing and hosted mode read local
  state only.

Each check returns ``{"name", "status", "detail"}`` with status one of
``ok`` | ``degraded`` | ``down`` | ``off``. Components that are simply
unconfigured report ``off``; degraded means usable-with-fallback; down
means broken. ``detail`` is human text with no secrets.
"""

from __future__ import annotations

from typing import Any

import httpx

STATUSES = ("ok", "degraded", "down", "off")

_OLLAMA_TIMEOUT_S = 2.0


def _item(name: str, status: str, detail: str) -> dict[str, str]:
    """One component row (status is always a known value)."""
    if status not in STATUSES:
        status = "down"
    return {"name": name, "status": status, "detail": detail}


def check_tier1(settings: Any = None) -> dict[str, str]:
    """Self-test: a planted canary must be detected by the code scanner."""
    try:
        from . import tier1
        from .canary import DEFAULT_KINDS, CanaryRegistry
        from .config import get_settings
        from .policy import load_policy

        cfg = settings or get_settings()
        registry = CanaryRegistry(cfg.canary_seed, DEFAULT_KINDS)
        canary = registry.value(DEFAULT_KINDS[0])
        spans = tier1.scan(f"password: {canary}", load_policy(), registry)
        if any(s.subclass == "canary" for s in spans):
            return _item("tier1", "ok", "self-test detected the planted canary")
        return _item(
            "tier1", "down",
            "self-test failed: the planted canary was not detected",
        )
    except Exception as exc:
        return _item(
            "tier1", "down", f"self-test error: {type(exc).__name__}"
        )


def _tags_match(models: list[str], wanted: str) -> bool:
    """True when the wanted model (or its base name) is in the tag list."""
    base = (wanted or "").strip().split(":")[0]
    for name in models:
        if not isinstance(name, str):
            continue
        if name == wanted or name.split(":")[0] == base and base:
            return True
    return False


def _hosted_now(settings=None):
    tmp = settings
    try:
        if tmp is not None and bool(getattr(tmp, "hosted", False)):
            return True
    except Exception:
        pass
    try:
        from . import hosted as hosted_module

        return bool(hosted_module.is_hosted())
    except Exception:
        return False


def check_ollama(
    settings: Any = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, str]:
    """Loopback only: is Ollama up, and is the Glimmer judge model listed?"""
    try:
        from .config import get_settings

        cfg = settings or get_settings()
        if _hosted_now(cfg):
            return _item(
                "ollama", "off",
                "Not used in the public demo \u2014 Muse Spark does the Tier 2 check",
            )
        if cfg.tier2_mode == "off":
            return _item(
                "ollama", "off",
                "local judge disabled (SF_TIER2_MODE=off)",
            )
        base = cfg.ollama_url.rstrip("/")
        with httpx.Client(
            transport=transport, timeout=_OLLAMA_TIMEOUT_S
        ) as client:
            version = client.get(base + "/api/version")
        if version.status_code != 200:
            return _item(
                "ollama", "down",
                f"Ollama answered status {version.status_code}",
            )
        try:
            with httpx.Client(
                transport=transport, timeout=_OLLAMA_TIMEOUT_S
            ) as client:
                tags = client.get(base + "/api/tags")
            payload = tags.json()
            models = [
                m.get("name", "")
                for m in (payload.get("models", []) or [])
                if isinstance(m, dict)
            ]
        except Exception:
            models = []
        if _tags_match(models, cfg.judge_model):
            return _item(
                "ollama", "ok",
                f"Ollama reachable; {cfg.judge_model} listed",
            )
        return _item(
            "ollama", "degraded",
            f"Ollama reachable; {cfg.judge_model} is not listed",
        )
    except Exception as exc:
        return _item(
            "ollama", "down", f"Ollama unreachable: {type(exc).__name__}"
        )


def check_meta_key(settings: Any = None) -> dict[str, str]:
    """Is a Meta key present? Reports presence only, never the value."""
    try:
        from .config import get_settings

        cfg = settings or get_settings()
        if cfg.meta_available:
            return _item(
                "meta-key", "ok",
                f"Meta key configured (model {cfg.meta_model})",
            )
        return _item("meta-key", "off", "no Meta key stored")
    except Exception as exc:
        return _item(
            "meta-key", "down", f"Meta key check failed: {type(exc).__name__}"
        )


def check_voice(settings: Any = None) -> dict[str, str]:
    """Is Muse Voice transcription configured? No audio, no network."""
    try:
        from .config import get_settings
        from .transcribe import TRANSCRIBE_MODEL

        cfg = settings or get_settings()
        if cfg.meta_available:
            return _item(
                "voice", "ok",
                f"Muse Voice ready ({TRANSCRIBE_MODEL})",
            )
        return _item(
            "voice", "off", "Muse Voice unavailable: no Meta key stored"
        )
    except Exception as exc:
        return _item(
            "voice", "down", f"Voice check failed: {type(exc).__name__}"
        )


def check_keystore(settings: Any = None) -> dict[str, str]:
    """OS credential backend available, or the ``.env`` fallback?"""
    try:
        from . import providers as providers_module

        if _hosted_now(settings):
            return _item(
                "keystore", "off",
                "Not used in the public demo \u2014 the key comes from the host's secret store",
            )
        backend = providers_module.storage_backend()
        if backend == "keyring":
            return _item(
                "keystore", "ok", "OS credential store available (keyring)"
            )
        return _item(
            "keystore", "degraded",
            "no OS credential backend; .env fallback in use",
        )
    except Exception as exc:
        return _item(
            "keystore", "degraded",
            f"keystore check failed: {type(exc).__name__}",
        )


def check_log(settings: Any = None) -> dict[str, str]:
    """Is the hash-chained decision log intact? Reads the file only."""
    try:
        from . import ecs_log
        from .config import get_settings

        cfg = settings or get_settings()
        ok, count = ecs_log.verify(cfg.log_path)
        noun = "event" if count == 1 else "events"
        if ok:
            return _item(
                "log", "ok", f"log chain verified · {count} {noun}"
            )
        return _item(
            "log", "down", f"log chain broken · {count} {noun}"
        )
    except Exception as exc:
        return _item(
            "log", "down", f"log verify failed: {type(exc).__name__}"
        )


def check_extension(settings: Any = None) -> dict[str, str]:
    """Is the browser-extension pairing token set? Never returns it."""
    try:
        from .config import ext_token_path, get_settings

        cfg = settings or get_settings()
        if _hosted_now(cfg):
            return _item(
                "extension", "off",
                "Not used in the public demo \u2014 the extension pairs with the app on your own PC",
            )
        if (cfg.ext_token or "").strip():
            return _item(
                "extension", "ok", "pairing token set via environment"
            )
        try:
            paired = ext_token_path(cfg).is_file()
        except OSError:
            paired = False
        if paired:
            return _item(
                "extension", "ok", "pairing token file present"
            )
        return _item(
            "extension", "off", "not paired yet; open the console to pair"
        )
    except Exception as exc:
        return _item(
            "extension", "down",
            f"pairing check failed: {type(exc).__name__}",
        )


def check_hosted() -> dict[str, str]:
    """Hosted demo mode on or off? Local flag read only."""
    try:
        from . import hosted as hosted_module

        if hosted_module.is_hosted():
            return _item("hosted", "ok", "hosted demo mode on")
        return _item("hosted", "off", "local mode (hosted off)")
    except Exception as exc:
        return _item(
            "hosted", "down", f"hosted check failed: {type(exc).__name__}"
        )


def collect_components(
    settings: Any = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, str]]:
    """Run every check in display order; never raises, never calls a
    paid API, never returns a secret."""
    try:
        from .config import get_settings

        cfg = settings or get_settings()
    except Exception:
        cfg = None
    return [
        check_tier1(cfg),
        check_ollama(cfg, transport=transport),
        check_meta_key(cfg),
        check_voice(cfg),
        check_keystore(cfg),
        check_log(cfg),
        check_extension(cfg),
        check_hosted(),
    ]
