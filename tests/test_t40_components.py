"""Component status (T40): local checks, status mapping, no secrets/network.

The endpoint must never return a secret, every check failure must map to
the documented status, and nothing here may touch the network: Ollama is
driven through an httpx MockTransport and Tier 2 stays off otherwise.
"""

from __future__ import annotations

import json

import httpx
import pytest

from shadowfenced import health as health_module
from shadowfenced.app import app, health_components
from shadowfenced.config import Settings

NAMES = [
    "tier1", "ollama", "meta-key", "voice",
    "keystore", "log", "extension", "hosted",
]
ALLOWED = {"ok", "degraded", "down", "off"}

LIVE = Settings(tier2_mode="live", judge_model="muse-glimmer:30b")


@pytest.fixture()
def components_transport():
    """Inject an httpx transport into the endpoint (removed afterwards)."""
    transports: list = []

    def use(transport) -> None:
        app.state.components_transport = transport
        transports.append(transport)

    yield use
    if hasattr(app.state, "components_transport"):
        delattr(app.state, "components_transport")


def _ollama_transport(
    models: list[str] | None = None,
    version_status: int = 200,
    fail: Exception | None = None,
) -> httpx.MockTransport:
    """Loopback Ollama stub: /api/version plus /api/tags, nothing else."""

    def handler(request: httpx.Request) -> httpx.Response:
        if fail is not None:
            raise fail
        if request.url.path == "/api/version":
            return httpx.Response(version_status, json={"version": "0.0.0"})
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": name} for name in (models or [])]},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _by_name(body) -> dict[str, dict]:
    return {entry["name"]: entry for entry in body["components"]}


def test_route_registered():
    table = {
        getattr(r, "path", ""): sorted(getattr(r, "methods", []) or [])
        for r in app.routes
    }
    assert table["/api/health/components"] == ["GET"]


def test_endpoint_shape_and_order(components_transport):
    components_transport(_ollama_transport())
    body = health_components()
    assert [entry["name"] for entry in body["components"]] == NAMES
    for entry in body["components"]:
        assert set(entry) == {"name", "status", "detail"}
        assert entry["status"] in ALLOWED
        assert isinstance(entry["detail"], str) and entry["detail"]


def test_endpoint_never_returns_secrets(monkeypatch, components_transport):
    """Sentinel key and token values must not appear anywhere in output."""
    monkeypatch.setenv("META_API_KEY", "SENTINEL-META-KEY-9f8e7d6c5b4a")
    monkeypatch.setenv("SHADOWFENCED_EXT_TOKEN", "SENTINEL-EXT-TOKEN-1a2b3c")
    components_transport(_ollama_transport())
    text = json.dumps(health_components())
    assert "SENTINEL-META-KEY" not in text
    assert "SENTINEL-EXT-TOKEN" not in text
    assert "Bearer" not in text


def test_endpoint_makes_no_network_calls(monkeypatch, components_transport):
    """With Tier 2 off, blocking httpx still returns every component."""
    def _forbidden(*args, **kwargs):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(httpx, "Client", _forbidden)
    body = health_components()
    assert [entry["name"] for entry in body["components"]] == NAMES


def test_only_loopback_hosts_are_contacted():
    """Even the live path may only ask loopback Ollama, never a paid API."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.0.0"})
        return httpx.Response(200, json={"models": []})

    health_module.collect_components(
        settings=LIVE, transport=httpx.MockTransport(handler)
    )
    assert seen, "expected at least one loopback request"
    for url in seen:
        host = httpx.URL(url).host
        assert host in ("127.0.0.1", "localhost"), url


def test_tier1_self_test_ok_and_failure(monkeypatch):
    import shadowfenced.tier1 as tier1_module

    assert health_module.check_tier1()["status"] == "ok"
    monkeypatch.setattr(tier1_module, "scan", lambda *a, **k: [])
    assert health_module.check_tier1()["status"] == "down"

    def _boom(*args, **kwargs):
        raise RuntimeError("broken")

    monkeypatch.setattr(tier1_module, "scan", _boom)
    assert health_module.check_tier1()["status"] == "down"


def test_ollama_status_mapping():
    off = health_module.check_ollama(
        Settings(tier2_mode="off"),
        transport=_ollama_transport(),
    )
    assert off["status"] == "off"
    ok = health_module.check_ollama(
        LIVE, transport=_ollama_transport(models=["muse-glimmer:30b"])
    )
    assert ok["status"] == "ok"
    degraded = health_module.check_ollama(
        LIVE, transport=_ollama_transport(models=["llama3.2:3b"])
    )
    assert degraded["status"] == "degraded"
    down = health_module.check_ollama(
        LIVE,
        transport=_ollama_transport(fail=httpx.ConnectError("down")),
    )
    assert down["status"] == "down"
    refused = health_module.check_ollama(
        LIVE, transport=_ollama_transport(version_status=500)
    )
    assert refused["status"] == "down"


def test_meta_key_and_voice_mapping():
    assert health_module.check_meta_key(Settings())["status"] == "off"
    assert health_module.check_voice(Settings())["status"] == "off"
    keyed = Settings(
        upstream="meta",
        meta_api_key="SENTINEL-KEY-zz9q8w7e",
        meta_base_url="http://meta.example",
        meta_model="m",
    )
    assert health_module.check_meta_key(keyed)["status"] == "ok"
    assert health_module.check_voice(keyed)["status"] == "ok"
    assert "SENTINEL-KEY" not in json.dumps(health_module.check_meta_key(keyed))


def test_keystore_mapping(monkeypatch):
    from shadowfenced import providers as providers_module

    monkeypatch.setattr(
        providers_module, "storage_backend", lambda: "keyring"
    )
    assert health_module.check_keystore()["status"] == "ok"
    monkeypatch.setattr(providers_module, "storage_backend", lambda: "env")
    assert health_module.check_keystore()["status"] == "degraded"


def test_log_mapping(tmp_path):
    missing = Settings(log_path=str(tmp_path / "absent.jsonl"))
    assert health_module.check_log(missing)["status"] == "ok"
    broken = tmp_path / "broken.jsonl"
    broken.write_text("not json\n", encoding="utf-8")
    assert (
        health_module.check_log(Settings(log_path=str(broken)))["status"]
        == "down"
    )


def test_extension_mapping(monkeypatch, tmp_path):
    assert (
        health_module.check_extension(Settings(ext_token="tok"))["status"]
        == "ok"
    )
    absent = tmp_path / "ext_token"
    assert (
        health_module.check_extension(
            Settings(ext_token="", ext_token_file=str(absent))
        )["status"]
        == "off"
    )
    absent.write_text("pairing-token\n", encoding="utf-8")
    paired = health_module.check_extension(
        Settings(ext_token="", ext_token_file=str(absent))
    )
    assert paired["status"] == "ok"
    assert "pairing-token" not in json.dumps(paired)


def test_hosted_mapping(monkeypatch):
    assert health_module.check_hosted()["status"] in ("ok", "off")
    monkeypatch.setenv("SF_HOSTED", "1")
    assert health_module.check_hosted()["status"] == "ok"
    monkeypatch.setenv("SF_HOSTED", "off")
    assert health_module.check_hosted()["status"] == "off"


def test_endpoint_failure_mapping(
    monkeypatch, tmp_path, components_transport
):
    """One integration pass: degraded Ollama + broken log + hosted on."""
    monkeypatch.setenv("SF_TIER2_MODE", "live")
    monkeypatch.setenv("SF_HOSTED", "1")
    broken = tmp_path / "decisions.jsonl"
    broken.write_text("corrupt\n", encoding="utf-8")
    monkeypatch.setenv("SF_LOG_PATH", str(broken))
    components_transport(_ollama_transport(models=["llama3.2:3b"]))
    rows = _by_name(health_components())
    assert rows["ollama"]["status"] == "degraded"
    assert rows["log"]["status"] == "down"
    assert rows["hosted"]["status"] == "ok"
