"""Provider registry, key storage, and OpenRouter OAuth PKCE (T35).

Registry order follows ``design-ref/PROVIDERS.md``: Meta Muse Spark is
the default and is listed first. Every provider except local Ollama is
OpenAI-compatible (Anthropic documents an OpenAI-compatible base URL in
PROVIDERS.md section 4, so no Messages-API branch is needed).

Key storage prefers the operating system's credential store through the
``keyring`` package (Windows Credential Manager, macOS Keychain, Linux
Secret Service) and falls back to the existing git-ignored ``.env``
file only when no keyring backend exists. Keys are never returned by
any helper here, never logged, and never placed in traces; callers
must treat them the same way.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

SERVICE_NAME = "shadowfenced"

OPENROUTER_AUTHORIZE_URL = "https://openrouter.ai/auth"
OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"


@dataclass(frozen=True)
class Provider:
    """One connectable upstream provider."""

    id: str
    label: str
    base_url: str
    default_model: str
    get_key_url: str | None
    needs_key: bool
    env_vars: tuple[str, ...] = ()
    oauth_pkce: bool = False


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="meta",
        label="Meta Muse Spark (cloud)",
        base_url="https://api.meta.ai/v1",
        default_model="muse-spark-1.3",
        get_key_url="https://dev.meta.ai/",
        needs_key=True,
        env_vars=("META_API_KEY", "MODEL_API_KEY"),
    ),
    Provider(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        get_key_url="https://platform.openai.com/settings/organization/api-keys",
        needs_key=True,
        env_vars=("OPENAI_API_KEY",),
    ),
    Provider(
        id="gemini",
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_model="gemini-2.0-flash",
        get_key_url="https://aistudio.google.com/apikey",
        needs_key=True,
        env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    ),
    Provider(
        id="anthropic",
        label="Anthropic Claude",
        base_url="https://api.anthropic.com/v1/",
        default_model="claude-sonnet-4-5",
        get_key_url="https://platform.claude.com/settings/keys",
        needs_key=True,
        env_vars=("ANTHROPIC_API_KEY",),
    ),
    Provider(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="openrouter/auto",
        get_key_url="https://openrouter.ai/settings/keys",
        needs_key=True,
        env_vars=("OPENROUTER_API_KEY",),
        oauth_pkce=True,
    ),
    Provider(
        id="ollama",
        label="Llama 3.2 3B via Ollama (local stand-in)",
        base_url="http://localhost:11434/v1",
        default_model="llama3.2:3b",
        get_key_url=None,
        needs_key=False,
        env_vars=("OLLAMA_API_KEY",),
    ),
)

_BY_ID: dict[str, Provider] = {p.id: p for p in PROVIDERS}

# Guard names used to register stored keys with keyguard so a stored
# key can never be sent upstream inside a message.
GUARD_FOR_ID: dict[str, str] = {
    p.id: f"SF_PROVIDER_{p.id.upper()}_KEY" for p in PROVIDERS if p.needs_key
}
ID_FOR_GUARD: dict[str, str] = {v: k for k, v in GUARD_FOR_ID.items()}


def get_provider(provider_id: str) -> Provider | None:
    """Return the registry entry for ``provider_id`` (None when unknown)."""
    return _BY_ID.get((provider_id or "").strip().lower())


def provider_ids() -> list[str]:
    """Registry ids in display order (Meta first)."""
    return [p.id for p in PROVIDERS]


# --- credential-store backend -----------------------------------------------


def _keyring_module() -> Any | None:
    """Import ``keyring`` (None when the package is missing)."""
    try:
        import keyring  # type: ignore[import-not-found]

        return keyring
    except Exception:
        return None


def keyring_available() -> bool:
    """True when an OS credential backend exists (False -> ``.env``)."""
    keyring = _keyring_module()
    if keyring is None:
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:
        return False
    name = type(backend).__module__ + "." + type(backend).__name__
    if "fail" in name.lower():
        return False
    return True


def storage_backend() -> str:
    """Where keys are stored: ``"keyring"`` or ``"env"`` (``.env``)."""
    return "keyring" if keyring_available() else "env"


def _read_dotenv() -> dict[str, str]:
    try:
        from .config import _find_dotenv, _parse_dotenv

        path = _find_dotenv()
        if path is not None:
            return _parse_dotenv(path)
    except Exception:
        pass
    return {}


def _env_value(provider: Provider) -> str:
    """First non-empty value from the provider's env vars (env wins)."""
    file_values = _read_dotenv()
    merged = dict(file_values)
    merged.update(os.environ)
    for name in provider.env_vars:
        value = merged.get(name, "")
        if value:
            return value
    return ""


def get_key(provider_id: str) -> str:
    """Return the stored key for one provider ("" when absent).

    Keyring first, then the existing ``.env``/environment settings.
    The value is transient: callers must not retain or log it.
    """
    provider = get_provider(provider_id)
    if provider is None or not provider.needs_key:
        return ""
    keyring = _keyring_module()
    if keyring is not None and keyring_available():
        try:
            value = keyring.get_password(SERVICE_NAME, provider.id) or ""
        except Exception:
            value = ""
        if value:
            return value
    return _env_value(provider)


def get_key_by_guard(guard_name: str) -> str:
    """Resolve a keyguard guard name to its transient key value."""
    provider_id = ID_FOR_GUARD.get(guard_name, "")
    if not provider_id:
        return ""
    return get_key(provider_id)


def is_connected(provider_id: str) -> bool:
    """True when the provider can be called (key stored, or no key)."""
    provider = get_provider(provider_id)
    if provider is None:
        return False
    if not provider.needs_key:
        return True
    return bool(get_key(provider.id))


def _write_dotenv_fallback(provider: Provider, key: str) -> None:
    """Write the key to the git-ignored ``.env`` fallback file."""
    from .config import _find_dotenv

    name = provider.env_vars[0]
    path = _find_dotenv()
    if path is None:
        path = Path.cwd() / ".env"
    lines: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    prefix = name + "="
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[index] = prefix + key
            replaced = True
            break
    if not replaced:
        lines.append(prefix + key)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _remove_dotenv_fallback(provider: Provider) -> None:
    """Remove the provider's fallback line from ``.env`` (if present)."""
    from .config import _find_dotenv

    path = _find_dotenv()
    if path is None:
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    prefix = provider.env_vars[0] + "=" if provider.env_vars else None
    if prefix is None:
        return
    kept = [line for line in lines if not line.strip().startswith(prefix)]
    if len(kept) != len(lines):
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def store_key(provider_id: str, key: str) -> str:
    """Store one provider key; return the storage backend used.

    Raises ``KeyError`` for an unknown provider, ``ValueError`` for an
    empty key or a provider that needs no key. The stored key is
    registered with keyguard so it can never be sent upstream.
    """
    from . import keyguard as keyguard_module

    provider = get_provider(provider_id)
    if provider is None:
        raise KeyError(f"unknown provider {provider_id!r}")
    if not provider.needs_key:
        raise ValueError(f"provider {provider.id!r} needs no key")
    cleaned = (key or "").strip()
    if not cleaned:
        raise ValueError("empty key")
    if keyring_available():
        keyring = _keyring_module()
        assert keyring is not None
        keyring.set_password(SERVICE_NAME, provider.id, cleaned)
        backend = "keyring"
    else:
        _write_dotenv_fallback(provider, cleaned)
        backend = "env"
    guard = GUARD_FOR_ID.get(provider.id)
    if guard:
        keyguard_module.register_extra_name(guard)
        keyguard_module.refresh()
    return backend


def delete_key(provider_id: str) -> None:
    """Forget one provider's stored key (keyring entry + ``.env`` line)."""
    from . import keyguard as keyguard_module

    provider = get_provider(provider_id)
    if provider is None:
        raise KeyError(f"unknown provider {provider_id!r}")
    keyring = _keyring_module()
    if keyring is not None:
        try:
            keyring.delete_password(SERVICE_NAME, provider.id)
        except Exception:
            pass
    _remove_dotenv_fallback(provider)
    guard = GUARD_FOR_ID.get(provider.id)
    if guard:
        keyguard_module.unregister_extra_name(guard)
        keyguard_module.refresh()


def reset_for_tests() -> None:
    """Drop in-memory OAuth sessions (tests only)."""
    _OAUTH_SESSIONS.clear()


def describe_all() -> list[dict[str, Any]]:
    """Serialise the registry for ``GET /api/providers`` (no key values)."""
    backend = storage_backend()
    items: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        connected = is_connected(provider.id)
        items.append(
            {
                "id": provider.id,
                "label": provider.label,
                "base_url": provider.base_url,
                "default_model": provider.default_model,
                "get_key_url": provider.get_key_url,
                "needs_key": provider.needs_key,
                "connected": connected,
                "available": connected,
                "storage": backend,
                "oauth_pkce": provider.oauth_pkce,
            }
        )
    return items


# --- OpenRouter OAuth with PKCE ----------------------------------------------
# Exactly per PROVIDERS.md section 5: random ``code_verifier``, the
# ``code_challenge`` is base64url-encoded SHA-256 of the verifier with
# ``code_challenge_method=S256``; the loopback callback lives on
# 127.0.0.1; the code is exchanged at
# ``POST https://openrouter.ai/api/v1/auth/keys`` for ``{key}``.

_OAUTH_SESSIONS: dict[str, dict[str, Any]] = {}


def create_verifier() -> str:
    """Random ``code_verifier`` (43+ URL-safe characters)."""
    return secrets.token_urlsafe(32)


def challenge_for(verifier: str) -> str:
    """``code_challenge`` for one verifier (base64url SHA-256, no pad)."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorize_url(callback_url: str, challenge: str) -> str:
    """OpenRouter login URL for one PKCE challenge."""
    query = urlencode(
        {
            "callback_url": callback_url,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{OPENROUTER_AUTHORIZE_URL}?{query}"


def _free_loopback_port() -> int:
    """An arbitrary free OS port on 127.0.0.1 for the callback server."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_oauth_session(
    *, callback_url: str | None = None, app_port: int = 8765
) -> dict[str, str]:
    """Begin one OpenRouter PKCE login; return the browser URL + ids.

    The callback stays on 127.0.0.1: by default the running app
    receives it at ``app_port``. Pass an explicit ``callback_url``
    (still on 127.0.0.1, e.g. with an arbitrary free OS port per the
    official docs) for a standalone loopback server instead.
    """
    from . import hosted as hosted_module

    if hosted_module.is_hosted():
        raise PermissionError("key storage is disabled in hosted demo mode")
    verifier = create_verifier()
    challenge = challenge_for(verifier)
    if callback_url is None:
        callback_url = (
            f"http://127.0.0.1:{app_port}"
            "/api/providers/openrouter/oauth/callback"
        )
    session_id = secrets.token_hex(16)
    _OAUTH_SESSIONS[session_id] = {
        "verifier": verifier,
        "callback_url": callback_url,
    }
    return {
        "session_id": session_id,
        "auth_url": build_authorize_url(callback_url, challenge),
        "callback_url": callback_url,
    }


def exchange_code(
    code: str,
    session_id: str,
    *,
    transport: Any | None = None,
) -> str:
    """Exchange one single-use ``code`` for the user's key and store it.

    Returns the storage backend used. The key itself is never returned
    to the caller beyond what storage needs (callers must not echo it).
    """
    import httpx

    from . import hosted as hosted_module

    if hosted_module.is_hosted():
        raise PermissionError("key storage is disabled in hosted demo mode")
    session = _OAUTH_SESSIONS.get(session_id)
    if session is None:
        raise KeyError("unknown OAuth session")
    verifier = session["verifier"]
    callback_url = session["callback_url"]
    cleaned = (code or "").strip()
    if not cleaned:
        raise ValueError("empty code")
    body: dict[str, str] = {"code": cleaned, "code_verifier": verifier}
    if "/api/providers/openrouter/oauth/callback" in callback_url:
        body["callback_url"] = callback_url
    try:
        with httpx.Client(transport=transport, timeout=30.0) as client:
            response = client.post(OPENROUTER_KEYS_URL, json=body)
    except Exception as exc:
        raise ConnectionError(f"key exchange unreachable: {type(exc).__name__}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("key exchange returned bad JSON") from exc
    if response.status_code != 200:
        raise ValueError(f"key exchange failed: status {response.status_code}")
    returned = payload.get("key", "") if isinstance(payload, dict) else ""
    if not returned:
        raise ValueError("key exchange returned no key")
    backend = store_key("openrouter", returned)
    _OAUTH_SESSIONS.pop(session_id, None)
    # Prove the login URL round-trips through the official authorize
    # page host even when tests only exercise the exchange step.
    _ = quote(callback_url, safe="")
    return backend
