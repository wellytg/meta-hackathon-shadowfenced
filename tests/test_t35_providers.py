"""Provider connections (T35): registry, key storage, test, OAuth PKCE.

No test touches the network: upstream goes through an httpx
``MockTransport`` and the OS credential store is a fake ``keyring``
module in ``sys.modules``. Key values are built at runtime and must
never appear in responses, logs, or traces.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import types

import httpx
import pytest
from fastapi import HTTPException

from shadowfenced import app as app_module
from shadowfenced import config as config_module
from shadowfenced import keyguard as keyguard_module
from shadowfenced import providers as providers_module
from shadowfenced.app import app
from shadowfenced.config import get_settings
from shadowfenced.upstream import UpstreamError, build_request


def _key(suffix: str) -> str:
    return "unit-test-key-" + suffix


class _FakeKeyring:
    """In-memory stand-in for the OS credential store."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_keyring(self) -> "_FakeKeyring":
        return self

    def set_password(self, service: str, user: str, password: str) -> None:
        self.store[(service, user)] = password

    def get_password(self, service: str, user: str) -> str | None:
        return self.store.get((service, user))

    def delete_password(self, service: str, user: str) -> None:
        try:
            del self.store[(service, user)]
        except KeyError:
            raise RuntimeError("no such password") from None


@pytest.fixture()
def fake_keyring(monkeypatch):
    """Route ``import keyring`` to an in-memory fake (with real cleanup)."""
    fake = _FakeKeyring()
    module = types.ModuleType("keyring")
    module.get_keyring = fake.get_keyring  # type: ignore[attr-defined]
    module.set_password = fake.set_password  # type: ignore[attr-defined]
    module.get_password = fake.get_password  # type: ignore[attr-defined]
    module.delete_password = fake.delete_password  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", module)
    yield fake


@pytest.fixture()
def hermetic_env(monkeypatch):
    """Neutralise the developer .env and any real provider key env vars."""
    monkeypatch.setattr(config_module, "_find_dotenv", lambda: None)
    for name in (
        "META_API_KEY",
        "MODEL_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_API_KEY",
        "SF_UPSTREAM",
        "SF_HOSTED",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _clean_provider_state(hermetic_env):
    keyguard_module.reset()
    providers_module.reset_for_tests()
    app_module.reset_provider_override()
    yield
    keyguard_module.reset()
    providers_module.reset_for_tests()
    app_module.reset_provider_override()
    for attr in ("upstream_transport", "provider_test_transports"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def _log_text() -> str:
    with open(get_settings().log_path, encoding="utf-8") as handle:
        return handle.read()


def _mock_ok(answer: str = "ok", status: int = 200, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(
            status,
            json={"choices": [{"message": {"role": "assistant", "content": answer}}]},
        )

    return httpx.MockTransport(handler)


# --- registry ---------------------------------------------------------------


def test_registry_meta_first_with_required_fields():
    assert providers_module.provider_ids() == [
        "meta",
        "openai",
        "gemini",
        "anthropic",
        "openrouter",
        "ollama",
    ]
    for entry in providers_module.PROVIDERS:
        assert entry.id and entry.label and entry.base_url
        assert entry.default_model
        assert isinstance(entry.needs_key, bool)
    by_id = {entry.id: entry for entry in providers_module.PROVIDERS}
    assert by_id["meta"].base_url == "https://api.meta.ai/v1"
    assert by_id["meta"].default_model == "muse-spark-1.3"
    assert "generativelanguage" in by_id["gemini"].base_url
    assert by_id["anthropic"].base_url == "https://api.anthropic.com/v1/"
    assert by_id["openrouter"].oauth_pkce is True
    assert by_id["ollama"].needs_key is False
    assert by_id["openai"].get_key_url and "openai.com" in by_id["openai"].get_key_url


def test_providers_endpoint_adds_connected_fields(hermetic_env):
    body = app_module.providers()
    assert [p["id"] for p in body["providers"]][0] == "meta"
    for item in body["providers"]:
        assert "connected" in item and "needs_key" in item
        assert "get_key_url" in item and "storage" in item
        assert "default_model" in item and "base_url" in item
    by_id = {p["id"]: p for p in body["providers"]}
    assert by_id["meta"]["connected"] is False
    assert by_id["ollama"]["connected"] is True


# --- key storage ------------------------------------------------------------


def test_store_and_delete_key_fake_keyring(fake_keyring):
    secret = _key("openai-1")
    result = app_module.store_provider_key(
        "openai", app_module.ProviderKeyRequest(key=secret)
    )
    assert result == {"ok": True, "connected": True, "storage": "keyring"}
    assert secret not in json.dumps(result)
    assert ("shadowfenced", "openai") in fake_keyring.store
    assert providers_module.get_key("openai") == secret
    assert app_module.providers()["providers"][1]["connected"] is True

    deleted = app_module.delete_provider_key("openai")
    assert deleted == {"ok": True, "connected": False}
    assert providers_module.get_key("openai") == ""
    assert secret not in json.dumps(deleted)


def test_store_unknown_empty_and_keyless_rejected(fake_keyring):
    with pytest.raises(HTTPException) as caught:
        app_module.store_provider_key("nope", app_module.ProviderKeyRequest(key="x"))
    assert caught.value.status_code == 404
    with pytest.raises(HTTPException) as caught:
        app_module.store_provider_key("openai", app_module.ProviderKeyRequest(key="  "))
    assert caught.value.status_code == 422
    with pytest.raises(HTTPException) as caught:
        app_module.store_provider_key("ollama", app_module.ProviderKeyRequest(key="x"))
    assert caught.value.status_code == 422


def test_env_fallback_when_no_keyring(monkeypatch, tmp_path):
    monkeypatch.setattr(providers_module, "keyring_available", lambda: False)
    dotenv = tmp_path / ".env"
    monkeypatch.setattr(config_module, "_find_dotenv", lambda: dotenv)
    secret = _key("fallback-1")
    result = app_module.store_provider_key(
        "openai", app_module.ProviderKeyRequest(key=secret)
    )
    assert result["storage"] == "env"
    assert dotenv.read_text(encoding="utf-8").count("OPENAI_API_KEY=") == 1
    assert providers_module.get_key("openai") == secret
    by_id = {p["id"]: p for p in app_module.providers()["providers"]}
    assert by_id["openai"]["storage"] == "env"
    app_module.delete_provider_key("openai")
    assert providers_module.get_key("openai") == ""
    assert "OPENAI_API_KEY" not in dotenv.read_text(encoding="utf-8")


# --- keys never echoed, logged, or traced ------------------------------------


def test_keyguard_learns_stored_key(fake_keyring):
    secret = _key("guard-1")
    app_module.store_provider_key("openai", app_module.ProviderKeyRequest(key=secret))
    found = keyguard_module.find_in_text(f"prefix {secret} suffix")
    assert found is not None
    assert found[0] == "SF_PROVIDER_OPENAI_KEY"
    assert found[1] == "exact"
    names = [entry["name"] for entry in keyguard_module.snapshot()]
    assert "SF_PROVIDER_OPENAI_KEY" in names
    state = "".join(
        str(value)
        for key, value in vars(keyguard_module).items()
        if not key.startswith("__")
    )
    assert secret not in state


def test_stored_key_blocked_before_upstream(fake_keyring):
    from shadowfenced.app import ChatRequest, chat_completions

    secret = _key("block-1")
    app_module.store_provider_key("openai", app_module.ProviderKeyRequest(key=secret))
    calls: list[httpx.Request] = []
    app.state.upstream_transport = _mock_ok(calls=calls)
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[{"role": "user", "content": f"here is {secret} ok"}],
        )
    )
    assert response.status_code == 403
    assert calls == []
    raw = response.body.decode("utf-8")
    assert secret not in raw
    assert secret not in _log_text()
    status = app_module.keyguard_status()
    assert secret not in json.dumps(status)


def test_forwarding_keeps_gated_path_for_new_provider(fake_keyring):
    from shadowfenced.app import SendRequest, send

    secret = _key("forward-1")
    app_module.store_provider_key("openai", app_module.ProviderKeyRequest(key=secret))
    calls: list[httpx.Request] = []
    app.state.upstream_transport = _mock_ok("noted [PII_EMAIL_1], thanks", calls=calls)
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": "mail ada@corvanelabs.com now"}],
            provider="openai",
        )
    )
    assert body["provider"] == "openai"
    assert body["decision"] == "REDACT"
    forwarded = json.loads(calls[0].content.decode("utf-8"))
    assert "ada@corvanelabs.com" not in json.dumps(forwarded)
    assert str(calls[0].url) == "https://api.openai.com/v1/chat/completions"
    assert calls[0].headers.get("authorization", "").startswith("Bearer ")
    assert secret not in json.dumps(forwarded)


def test_build_request_new_providers_need_keys(fake_keyring):
    from shadowfenced.config import Settings

    secret = _key("shape-1")
    app_module.store_provider_key("openai", app_module.ProviderKeyRequest(key=secret))
    url, headers, payload = build_request(
        [{"role": "user", "content": "hi"}], "openai", Settings()
    )
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers == {"Authorization": "Bearer " + secret}
    assert payload["model"] == "gpt-4o-mini"
    with pytest.raises(UpstreamError):
        build_request([{"role": "user", "content": "hi"}], "gemini", Settings())


def test_meta_uses_stored_key_when_env_empty(fake_keyring):
    secret = _key("meta-1")
    app_module.store_provider_key("meta", app_module.ProviderKeyRequest(key=secret))
    settings = get_settings()
    assert settings.meta_api_key == secret
    assert settings.meta_available is True
    url, headers, payload = build_request(
        [{"role": "user", "content": "hi"}], "meta", settings
    )
    assert url == "https://api.meta.ai/v1/chat/completions"
    assert headers == {"Authorization": "Bearer " + secret}
    assert payload["model"] == "muse-spark-1.3"


def test_set_provider_accepts_registry_ids():
    assert app_module.set_provider(app_module.ProviderRequest(id="openai"))["default"] == "openai"
    assert app_module.providers()["default"] == "openai"
    with pytest.raises(HTTPException) as caught:
        app_module.set_provider(app_module.ProviderRequest(id="nope"))
    assert caught.value.status_code == 404


# --- test endpoint: tiny request, clean errors --------------------------------


def test_connection_probe_ok_and_clean_errors(fake_keyring):
    secret = _key("probe-1")
    app_module.store_provider_key("openai", app_module.ProviderKeyRequest(key=secret))

    app.state.upstream_transport = _mock_ok()
    body = app_module.check_provider_connection("openai")
    assert body == {"ok": True, "message": "connection ok"}

    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key " + secret})

    app.state.upstream_transport = httpx.MockTransport(unauthorized)
    body = app_module.check_provider_connection("openai")
    assert body["ok"] is False
    assert secret not in body["message"]

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    app.state.upstream_transport = httpx.MockTransport(down)
    body = app_module.check_provider_connection("openai")
    assert body == {"ok": False, "message": "connection failed: ConnectError"}

    app_module.delete_provider_key("openai")
    app.state.upstream_transport = _mock_ok()
    body = app_module.check_provider_connection("openai")
    assert body == {"ok": False, "message": "no API key stored"}

    with pytest.raises(HTTPException) as caught:
        app_module.check_provider_connection("nope")
    assert caught.value.status_code == 404


# --- OpenRouter OAuth PKCE -----------------------------------------------------


def test_pkce_verifier_and_challenge():
    verifier = providers_module.create_verifier()
    assert len(verifier) >= 43
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    ).decode("ascii").rstrip("=")
    assert providers_module.challenge_for(verifier) == expected
    url = providers_module.build_authorize_url("http://127.0.0.1:9/callback", expected)
    assert url.startswith("https://openrouter.ai/auth?")
    assert "code_challenge_method=S256" in url
    assert "code_challenge=" in url
    assert "callback_url=" in url


def test_oauth_start_callback_on_loopback(fake_keyring):
    started = providers_module.start_oauth_session(
        callback_url="http://127.0.0.1:9/callback"
    )
    assert started["callback_url"] == "http://127.0.0.1:9/callback"
    assert started["callback_url"].startswith("http://127.0.0.1:")
    assert started["auth_url"].startswith("https://openrouter.ai/auth?")
    assert started["session_id"]


def test_oauth_exchange_stores_key(fake_keyring):
    secret = _key("oauth-1")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"key": secret})

    app.state.provider_test_transports = {"openrouter": httpx.MockTransport(handler)}
    started = app_module.start_openrouter_oauth()
    assert started["auth_url"].startswith("https://openrouter.ai/auth?")
    session_id = started["session_id"]
    stored = {"session": session_id}
    session = providers_module._OAUTH_SESSIONS[session_id]
    result = app_module.openrouter_oauth_callback(code="one-use-code", session_id=session_id)
    assert result == {"ok": True, "connected": True, "storage": "keyring"}
    assert secret not in json.dumps(result)
    assert seen["url"] == "https://openrouter.ai/api/v1/auth/keys"
    assert seen["body"]["code"] == "one-use-code"
    assert seen["body"]["code_verifier"] == session["verifier"]
    assert seen["body"]["callback_url"].startswith("http://127.0.0.1:")
    assert providers_module.get_key("openrouter") == secret
    assert stored["session"] not in providers_module._OAUTH_SESSIONS
    found = keyguard_module.find_in_text(f"oops {secret}")
    assert found is not None and found[0] == "SF_PROVIDER_OPENROUTER_KEY"


def test_oauth_headless_exchange_variant(fake_keyring):
    secret = _key("oauth-2")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"key": secret})

    app.state.provider_test_transports = {"openrouter": httpx.MockTransport(handler)}
    started = providers_module.start_oauth_session(
        callback_url="http://127.0.0.1:9/callback"
    )
    result = app_module.exchange_openrouter_oauth(
        app_module.ProviderOAuthExchange(
            code="pasted-code", session_id=started["session_id"]
        )
    )
    assert result["connected"] is True
    assert providers_module.get_key("openrouter") == secret


def test_oauth_exchange_failure_is_clean(fake_keyring):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad code"})

    app.state.provider_test_transports = {"openrouter": httpx.MockTransport(handler)}
    started = providers_module.start_oauth_session(
        callback_url="http://127.0.0.1:9/callback"
    )
    with pytest.raises(HTTPException) as caught:
        app_module.openrouter_oauth_callback(
            code="stale-code", session_id=started["session_id"]
        )
    assert caught.value.status_code == 502
    assert providers_module.get_key("openrouter") == ""


# --- hosted demo mode ----------------------------------------------------------


def test_hosted_mode_refuses_key_storage(monkeypatch, fake_keyring):
    monkeypatch.setenv("SF_HOSTED", "1")
    with pytest.raises(HTTPException) as caught:
        app_module.store_provider_key("openai", app_module.ProviderKeyRequest(key="x"))
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:
        app_module.delete_provider_key("openai")
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:
        app_module.start_openrouter_oauth()
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:
        app_module.openrouter_oauth_callback(code="c", session_id="s")
    assert caught.value.status_code == 403
    assert fake_keyring.store == {}
