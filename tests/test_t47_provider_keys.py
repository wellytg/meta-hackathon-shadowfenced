"""T47 tests: current provider key shapes BLOCK plain and inside encodings.

Every key is generated at runtime from random characters; no real-format
literal ever appears in this file. Prefixes are assembled from fragments
so this file can never match its own patterns.
"""

from __future__ import annotations

import base64
import random
import secrets
import string
from collections.abc import Callable
from urllib.parse import quote

import httpx
import pytest

from shadowfenced.app import ScanRequest, SendRequest, app, scan, send

ALNUM = string.ascii_letters + string.digits
URLSAFE = ALNUM + "-_"
UPPER = string.ascii_uppercase + string.digits
B64ALPHA = ALNUM + "/+"


def _frag(*parts: str) -> str:
    return "".join(parts)


def _rand(alphabet: str, count: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(count))


def _aws_pair() -> str:
    key_id = _frag("AK", "IA") + _rand(UPPER, 16)
    # Leading non-hex char guarantees this is never a git SHA.
    secret = "G" + _rand(B64ALPHA, 39)
    return key_id + " " + secret


_BUILDERS: list[tuple[str, Callable[[], str]]] = [
    ("OpenAI", lambda: _frag("s", "k-") + _rand(ALNUM, 48)),
    ("OpenAI", lambda: _frag("s", "k-proj-") + _rand(URLSAFE, 150)),
    ("OpenAI", lambda: _frag("s", "k-svcacct-") + _rand(URLSAFE, 64)),
    ("OpenAI", lambda: _frag("s", "k-admin-") + _rand(URLSAFE, 64)),
    ("Anthropic", lambda: _frag("s", "k-ant-api03-") + _rand(URLSAFE, 64)),
    ("Anthropic", lambda: _frag("s", "k-ant-admin01-") + _rand(URLSAFE, 64)),
    ("Google", lambda: _frag("AI", "za") + _rand(ALNUM + "-_", 35)),
    ("Hugging Face", lambda: _frag("h", "f_") + _rand(ALNUM, 34)),
    ("Groq", lambda: _frag("gs", "k_") + _rand(ALNUM, 52)),
    ("xAI", lambda: _frag("xa", "i-") + _rand(ALNUM, 48)),
    ("OpenRouter", lambda: _frag("s", "k-or-v1-") + _rand(ALNUM, 64)),
    ("Stripe", lambda: _frag("s", "k_live_") + _rand(ALNUM, 24)),
    ("Stripe", lambda: _frag("r", "k_live_") + _rand(ALNUM, 24)),
    ("Slack", lambda: _frag("x", "oxa-") + _rand(ALNUM + "-", 24)),
    ("Slack", lambda: _frag("x", "oxb-") + _rand(ALNUM + "-", 24)),
    ("Slack", lambda: _frag("x", "oxp-") + _rand(ALNUM + "-", 24)),
    ("Slack", lambda: _frag("x", "oxr-") + _rand(ALNUM + "-", 24)),
    (
        "GitHub",
        lambda: _frag("github_", "pat_")
        + _rand(ALNUM, 22)
        + "_"
        + _rand(ALNUM, 59),
    ),
    ("AWS", lambda: _frag("AK", "IA") + _rand(UPPER, 16)),
    ("AWS", _aws_pair),
    ("Private key", lambda: _frag("-----BEGIN ", "PRIVATE KEY-----")),
]

_IDS = [f"{label}-{i}" for i, (label, _) in enumerate(_BUILDERS)]


@pytest.fixture()
def mock_upstream():
    calls: list = []

    def make(answer: str = "happy to help", status: int = 200) -> list:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                status,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": answer}}
                    ]
                },
            )

        app.state.upstream_transport = httpx.MockTransport(handler)
        return calls

    yield make
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")


def _provider_spans(payload: dict) -> list[dict]:
    return [
        span
        for span in payload["spans"]
        if span["subclass"] == "provider_key" and span["status"] == "proven"
    ]


def _expected_message(label: str) -> str:
    return f"A {label} API key \u2014 not sent"


@pytest.mark.parametrize("index", range(len(_BUILDERS)), ids=_IDS)
def test_plain_provider_key_blocks(index: int, mock_upstream) -> None:
    label, build = _BUILDERS[index]
    key = build()
    calls = mock_upstream()
    found = scan(ScanRequest(text=f"config holds {key} do not share"))
    hits = _provider_spans(found)
    assert hits, f"no proven provider_key span for {label}"
    assert all(span["suggested_action"] == "block" for span in hits)
    assert all(span["message"] == _expected_message(label) for span in hits)
    body = send(
        SendRequest(
            messages=[{"role": "user", "content": f"here is the key {key}"}]
        )
    )
    assert body["decision"] == "BLOCK"
    assert body["user_message"] == _expected_message(label)
    assert body["sent_text"] is None
    assert body["answer"] is None
    assert calls == []


def _encode(payload: str, encoding: str) -> str:
    raw = payload.encode("utf-8")
    if encoding == "base64":
        return base64.b64encode(raw).decode("ascii")
    if encoding == "hex":
        return raw.hex()
    assert encoding == "url"
    return quote(payload, safe="")


@pytest.mark.parametrize("index", range(len(_BUILDERS)), ids=_IDS)
@pytest.mark.parametrize("encoding", ["base64", "hex", "url"])
def test_encoded_provider_key_blocks(
    index: int, encoding: str, mock_upstream
) -> None:
    label, build = _BUILDERS[index]
    key = build()
    blob = _encode(_frag("KE", "Y=") + key, encoding)
    calls = mock_upstream()
    found = scan(ScanRequest(text=f"debug log: {blob}"))
    hits = _provider_spans(found)
    assert hits, f"no proven provider_key span for {label} inside {encoding}"
    assert all(span["suggested_action"] == "block" for span in hits)
    assert any(
        f"provider key inside {encoding}" in span["why"] for span in hits
    )
    (earliest,) = sorted(
        (span for span in hits if f"provider key inside {encoding}" in span["why"]),
        key=lambda span: (span["cp_start"], span["cp_end"]),
    )[:1]
    assert found["spans"] and earliest["message"] == _expected_message(label)
    # The span points at the encoded run in the original text.
    text = f"debug log: {blob}"
    assert text[earliest["cp_start"]:earliest["cp_end"]] == blob
    body = send(
        SendRequest(messages=[{"role": "user", "content": f"debug {blob}"}])
    )
    assert body["decision"] == "BLOCK"
    assert body["user_message"] == _expected_message(label)
    assert calls == []


def test_base64url_provider_key_blocks(mock_upstream) -> None:
    calls = mock_upstream()
    # A 4-byte UTF-8 prefix shifts the stream so the url-safe blob
    # carries "-" / "_" (pure ASCII payloads encode clean).
    key = _frag("s", "k-proj-") + _rand(URLSAFE, 150)
    blob = ""
    for _ in range(20):
        blob = base64.urlsafe_b64encode(
            ("\U0001F511" + _frag("KE", "Y=") + key).encode("utf-8")
        ).decode("ascii")
        if "-" in blob or "_" in blob:
            break
    assert "-" in blob or "_" in blob
    found = scan(ScanRequest(text=f"debug log: {blob}"))
    hits = _provider_spans(found)
    assert hits, "no proven provider_key span inside base64url"
    assert any(
        "provider key inside base64url" in span["why"] for span in hits
    ), [span["why"] for span in hits]
    body = send(
        SendRequest(messages=[{"role": "user", "content": f"debug {blob}"}])
    )
    assert body["decision"] == "BLOCK"
    assert body["user_message"] == _expected_message("OpenAI")
    assert calls == []


def test_nested_encoding_provider_key_blocks(mock_upstream) -> None:
    calls = mock_upstream()
    key = _frag("s", "k-proj-") + _rand(URLSAFE, 150)
    payload = _frag("KE", "Y=") + key
    hex_of_b64 = base64.b64encode(payload.encode("utf-8")).decode().encode().hex()
    found = scan(ScanRequest(text=f"debug log: {hex_of_b64}"))
    hits = _provider_spans(found)
    assert hits, "no proven provider_key span inside hex+base64"
    assert any(
        "provider key inside hex+base64" in span["why"] for span in hits
    ), [span["why"] for span in hits]
    assert (
        send(SendRequest(messages=[{"role": "user", "content": hex_of_b64}]))[
            "decision"
        ]
        == "BLOCK"
    )
    b64_of_url = base64.b64encode(quote(payload, safe="").encode()).decode()
    found = scan(ScanRequest(text=f"debug log: {b64_of_url}"))
    hits = _provider_spans(found)
    assert hits, "no proven provider_key span inside base64+url"
    assert any(
        "provider key inside base64+url" in span["why"] for span in hits
    ), [span["why"] for span in hits]
    assert (
        send(SendRequest(messages=[{"role": "user", "content": b64_of_url}]))[
            "decision"
        ]
        == "BLOCK"
    )
    assert calls == []


def test_base64_image_bytes_do_not_block(mock_upstream) -> None:
    mock_upstream()
    rng = random.Random(20261101)
    raw = bytes.fromhex("89504e470d0a1a0a") + bytes(
        rng.randrange(256) for _ in range(64)
    )
    blob = base64.b64encode(raw).decode("ascii")
    found = scan(ScanRequest(text=f"image data {blob} end"))
    assert _provider_spans(found) == []
    body = send(
        SendRequest(messages=[{"role": "user", "content": f"image {blob}"}])
    )
    assert body["decision"] != "BLOCK"


def test_base64_plain_text_does_not_block(mock_upstream) -> None:
    mock_upstream()
    blob = base64.b64encode(
        b"the quick brown fox jumps over and over and over"
    ).decode("ascii")
    found = scan(ScanRequest(text=f"note {blob} end"))
    assert _provider_spans(found) == []
    body = send(
        SendRequest(messages=[{"role": "user", "content": f"note {blob}"}])
    )
    assert body["decision"] != "BLOCK"


def test_git_sha_and_uuid_do_not_block(mock_upstream) -> None:
    mock_upstream()
    sha = secrets.token_hex(20)
    uid = (
        f"{secrets.token_hex(4)}-{secrets.token_hex(2)}"
        f"-{secrets.token_hex(2)}-{secrets.token_hex(2)}"
        f"-{secrets.token_hex(6)}"
    )
    text = f"commit {sha} run {uid} done"
    found = scan(ScanRequest(text=text))
    assert _provider_spans(found) == []
    body = send(SendRequest(messages=[{"role": "user", "content": text}]))
    assert body["decision"] != "BLOCK"


def test_sk_inside_words_does_not_block(mock_upstream) -> None:
    mock_upstream()
    text = "the flask-based task scheduler masks sensitive details at the disk shop"
    found = scan(ScanRequest(text=text))
    assert _provider_spans(found) == []
    body = send(SendRequest(messages=[{"role": "user", "content": text}]))
    assert body["decision"] != "BLOCK"
