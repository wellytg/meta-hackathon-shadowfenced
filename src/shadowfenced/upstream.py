"""Upstream chat: forward redacted text to any OpenAI-compatible AI.

Ollama goes to ``{OLLAMA_URL}/v1/chat/completions`` with the local model;
Meta goes to ``{META_BASE_URL}/chat/completions`` with a Bearer key.
Anything failing (network, status, shape) raises ``UpstreamError`` so
callers can fail closed. A test may inject an ``httpx`` transport
(``MockTransport`` works with both the sync and async clients).
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Settings, get_settings

TIMEOUT_S = 60.0

# Meta reasons first (hundreds of reasoning tokens before any content),
# so it gets a longer timeout than the local Ollama stand-in.
META_TIMEOUT_S = 90.0


def _verify_for(url: str) -> Any:
    """TLS verification for one upstream URL (never disabled).

    HTTPS upstreams verify through the OS store via ``truststore`` (the
    Windows store holds enterprise re-signing roots that certifi lacks);
    when ``truststore`` cannot be imported, httpx's default verification
    applies. Plain HTTP needs no TLS context.
    """
    if not url.lower().startswith("https://"):
        return True
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return True


def _timeout_for(provider: str | None, timeout: float,
                 settings: Settings) -> float:
    """Effective timeout: Meta gets 90 s unless the caller overrode it."""
    if timeout != TIMEOUT_S:
        return timeout
    name = (provider or settings.upstream or "ollama").lower()
    if name == "meta":
        return META_TIMEOUT_S
    return timeout


class UpstreamError(RuntimeError):
    """The upstream AI could not produce an answer."""


def build_request(
    messages: list[dict[str, Any]],
    provider: str | None,
    settings: Settings,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Return (url, headers, json body) for one provider."""
    name = (provider or settings.upstream or "ollama").lower()
    if name == "meta":
        if not settings.meta_available:
            raise UpstreamError("meta upstream unavailable")
        url = settings.meta_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": "Bearer " + settings.meta_api_key}
        # No max_tokens: the Meta model reasons first (hundreds of
        # reasoning tokens), so a small cap returns empty content.
        body: dict[str, Any] = {"model": settings.meta_model, "messages": messages}
        return url, headers, body
    if name == "ollama":
        url = settings.ollama_url.rstrip("/") + "/v1/chat/completions"
        return url, {}, {"model": settings.local_model, "messages": messages}
    raise UpstreamError(f"unknown provider {provider!r}")


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") != "image_url"
        )
    return ""


def parse_answer(status_code: int, payload: Any) -> str:
    """Extract the answer text or raise ``UpstreamError``."""
    if status_code != 200:
        raise UpstreamError(f"upstream status {status_code}")
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise UpstreamError("upstream returned bad JSON") from exc
    text = _content_to_text(content)
    if not isinstance(text, str):
        raise UpstreamError("upstream returned non-text content")
    return text


def chat_sync(
    messages: list[dict[str, Any]],
    provider: str | None = None,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = TIMEOUT_S,
) -> str:
    """Synchronous full path (used by the sync app endpoints in tests)."""
    cfg = settings or get_settings()
    url, headers, body = build_request(messages, provider, cfg)
    try:
        with httpx.Client(
            transport=transport,
            timeout=_timeout_for(provider, timeout, cfg),
            verify=_verify_for(url),
        ) as client:
            response = client.post(url, json=body, headers=headers)
    except UpstreamError:
        raise
    except Exception as exc:
        raise UpstreamError(f"upstream unreachable: {type(exc).__name__}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstreamError("upstream returned bad JSON") from exc
    return parse_answer(response.status_code, payload)


async def chat(
    messages: list[dict[str, Any]],
    provider: str | None = None,
    *,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = TIMEOUT_S,
) -> str:
    """Send messages to the upstream AI and return the answer text."""
    cfg = settings or get_settings()
    url, headers, body = build_request(messages, provider, cfg)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=_timeout_for(provider, timeout, cfg),
            verify=_verify_for(url),
        ) as client:
            response = await client.post(url, json=body, headers=headers)
    except UpstreamError:
        raise
    except Exception as exc:
        raise UpstreamError(f"upstream unreachable: {type(exc).__name__}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstreamError("upstream returned bad JSON") from exc
    return parse_answer(response.status_code, payload)
