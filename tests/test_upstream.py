"""Provider + Meta upstream tests (T13).

No test touches the network: upstream goes through an httpx
MockTransport. Key values are built at runtime; nothing here is a real
provider key shape.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import HTTPException

from shadowfenced import app as app_module
from shadowfenced import config as config_module
from shadowfenced.app import (
    SendRequest,
    providers,
    reset_provider_override,
    send,
    set_provider,
)
from shadowfenced.app import app
from shadowfenced.config import Settings, get_settings
from shadowfenced.upstream import (
    META_TIMEOUT_S,
    TIMEOUT_S,
    UpstreamError,
    _timeout_for,
    _verify_for,
    build_request,
)


def _key(suffix: str = "x") -> str:
    return "unit-test-key-" + suffix


@pytest.fixture()
def hermetic_env(monkeypatch):
    """Neutralise the developer .env and any real key env vars."""
    monkeypatch.setattr(config_module, "_find_dotenv", lambda: None)
    for name in (
        "META_API_KEY",
        "MODEL_API_KEY",
        "META_BASE_URL",
        "META_MODEL",
        "SF_UPSTREAM",
        "OLLAMA_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _clean_provider():
    reset_provider_override()
    yield
    reset_provider_override()


@pytest.fixture()
def mock_upstream():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    app.state.upstream_transport = httpx.MockTransport(handler)
    yield calls
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")


def test_no_key_defaults_to_ollama(hermetic_env):
    settings = get_settings()
    assert settings.upstream == "ollama"
    assert settings.meta_available is False
    assert settings.meta_base_url == "https://api.meta.ai/v1"
    assert settings.meta_model == "muse-spark-1.3"


def test_model_api_key_alias(hermetic_env, monkeypatch):
    key = _key("alias")
    settings = get_settings({"MODEL_API_KEY": key})
    assert settings.meta_api_key == key
    assert settings.upstream == "meta"
    assert settings.meta_available is True


def test_meta_api_key_wins_over_alias(hermetic_env):
    settings = get_settings({"META_API_KEY": "a", "MODEL_API_KEY": "b"})
    assert settings.meta_api_key == "a"


def test_meta_unavailable_without_key(hermetic_env):
    assert get_settings({"SF_UPSTREAM": "meta"}).meta_available is False
    with pytest.raises(UpstreamError):
        build_request([], "meta", get_settings({"SF_UPSTREAM": "meta"}))


def test_meta_request_shape_url_bearer_model():
    key = _key("shape")
    settings = Settings(
        upstream="meta",
        meta_api_key=key,
        meta_base_url="http://meta.example",
        meta_model="m",
    )
    url, headers, body = build_request(
        [{"role": "user", "content": "hi"}], "meta", settings
    )
    assert url == "http://meta.example/chat/completions"
    assert headers == {"Authorization": "Bearer " + key}
    assert body["model"] == "m"
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_providers_lists_both_with_availability(hermetic_env):
    body = providers()
    assert body["default"] == "ollama"
    by_id = {p["id"]: p for p in body["providers"]}
    assert by_id["meta"]["label"] == "Meta Muse Spark (cloud)"
    assert by_id["meta"]["available"] is False
    assert by_id["ollama"]["label"] == "Llama 3.2 3B via Ollama (local stand-in)"
    assert by_id["ollama"]["available"] is True


def test_providers_meta_available_with_key(hermetic_env, monkeypatch):
    monkeypatch.setenv("META_API_KEY", _key("avail"))
    body = providers()
    by_id = {p["id"]: p for p in body["providers"]}
    assert by_id["meta"]["available"] is True
    assert body["default"] == "meta"


def test_set_provider_switches_default(hermetic_env):
    assert providers()["default"] == "ollama"
    body = set_provider(app_module.ProviderRequest(id="meta"))
    assert body["default"] == "meta"
    assert providers()["default"] == "meta"
    set_provider(app_module.ProviderRequest(id="ollama"))
    assert providers()["default"] == "ollama"


def test_set_provider_unknown_is_404(hermetic_env):
    with pytest.raises(HTTPException) as exc_info:
        set_provider(app_module.ProviderRequest(id="nope"))
    assert exc_info.value.status_code == 404


def test_send_accepts_provider_and_reports_it(
    hermetic_env, monkeypatch, mock_upstream
):
    monkeypatch.setenv("META_API_KEY", _key("send"))
    meta_body = send(
        SendRequest(
            messages=[{"role": "user", "content": "hello"}],
            provider="meta",
        )
    )
    assert meta_body["provider"] == "meta"
    assert meta_body["decision"] == "ALLOW"
    assert (
        str(mock_upstream[0].url)
        == "https://api.meta.ai/v1/chat/completions"
    )

    local_body = send(
        SendRequest(
            messages=[{"role": "user", "content": "hello"}],
            provider="ollama",
        )
    )
    assert local_body["provider"] == "ollama"
    assert (
        str(mock_upstream[1].url)
        == "http://127.0.0.1:11434/v1/chat/completions"
    )


def test_meta_body_omits_max_tokens():
    key = _key("nomax")
    settings = Settings(
        upstream="meta",
        meta_api_key=key,
        meta_base_url="http://meta.example",
        meta_model="m",
    )
    _, _, body = build_request(
        [{"role": "user", "content": "hi"}], "meta", settings
    )
    assert "max_tokens" not in body


def test_meta_default_timeout_is_90s(hermetic_env):
    settings = get_settings({"META_API_KEY": _key("t")})
    assert META_TIMEOUT_S == 90.0
    assert _timeout_for("meta", TIMEOUT_S, settings) == 90.0
    assert _timeout_for("ollama", TIMEOUT_S, settings) == TIMEOUT_S
    assert _timeout_for("meta", 5.0, settings) == 5.0


def test_verify_stays_on_for_https():
    import ssl

    verify = _verify_for("https://api.meta.ai/v1/chat/completions")
    try:
        import truststore

        assert isinstance(verify, ssl.SSLContext)
        assert isinstance(verify, truststore.SSLContext)
    except ImportError:
        assert verify is True
    assert _verify_for("http://127.0.0.1:11434/v1/chat/completions") is True


def test_send_default_follows_provider_override(
    hermetic_env, monkeypatch, mock_upstream
):
    monkeypatch.setenv("META_API_KEY", _key("default"))
    set_provider(app_module.ProviderRequest(id="meta"))
    body = send(SendRequest(messages=[{"role": "user", "content": "hello"}]))
    assert body["provider"] == "meta"
    assert (
        str(mock_upstream[0].url)
        == "https://api.meta.ai/v1/chat/completions"
    )
