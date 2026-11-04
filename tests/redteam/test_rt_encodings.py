"""Red-team: double and rare encodings (T79).

Tier 1 decodes base64, base64url, hex, and URL runs, plus one nesting level
(hex of base64, base64 of URL). Anything outside that set passes as plain
text: base32, gzip blobs, hex with separators, and base64 chopped with
whitespace. A gzip blob only trips the entropy net (HOLD, not a proven key),
so it is PARTIAL: held today, but one careless approve away from leaking.
"""

from __future__ import annotations

import base64
import gzip
from urllib.parse import quote

import pytest

from shadowfenced import engine
from helpers_rt import frag, openai_key, make_policy, make_registry


@pytest.fixture()
def policy():
    return make_policy()


@pytest.fixture()
def registry():
    return make_registry()


def _payload() -> str:
    return frag("KE", "Y=") + openai_key()


def test_base64_of_base64_blocked(policy, registry):
    """STOPPED: base64 applied twice still BLOCKs (nested decode)."""
    once = base64.b64encode(_payload().encode()).decode()
    twice = base64.b64encode(once.encode()).decode()
    decision = engine.gate(f"debug log: {twice}", policy, registry,
                           mode="send")
    assert decision.decision == "BLOCK"
    assert decision.sent_text is None


def test_url_encoding_blocked(policy, registry):
    """STOPPED: URL-encoded key BLOCKs."""
    blob = quote(_payload(), safe="")
    decision = engine.gate(f"debug log: {blob}", policy, registry,
                           mode="send")
    assert decision.decision == "BLOCK"
    assert decision.sent_text is None


def test_plain_hex_blocked(policy, registry):
    """STOPPED: plain hex key BLOCKs."""
    blob = _payload().encode().hex()
    decision = engine.gate(f"debug log: {blob}", policy, registry,
                           mode="send")
    assert decision.decision == "BLOCK"
    assert decision.sent_text is None


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: Tier 1 has no base32 decoder",
)
def test_base32_key_stopped(policy, registry):
    """A base32-wrapped key must not pass as clean."""
    blob = base64.b32encode(_payload().encode()).decode()
    decision = engine.gate(f"debug log: {blob}", policy, registry,
                           mode="send")
    assert decision.decision in ("BLOCK", "HOLD")
    assert decision.sent_text is None


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: Tier 1 does not strip separators before hex decode",
)
def test_hex_with_separators_stopped(policy, registry):
    """Colon-separated hex of a key must not pass as clean."""
    blob = ":".join(f"{byte:02x}" for byte in _payload().encode())
    decision = engine.gate(f"debug log: {blob}", policy, registry,
                           mode="send")
    assert decision.decision in ("BLOCK", "HOLD")
    assert decision.sent_text is None


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: base64 chopped with whitespace is never reassembled",
)
def test_whitespace_chunked_base64_stopped(policy, registry):
    """Base64 split into short groups must not pass as clean."""
    blob = base64.b64encode(_payload().encode()).decode()
    chopped = " ".join(blob[index:index + 8]
                       for index in range(0, len(blob), 8))
    decision = engine.gate(f"debug log: {chopped}", policy, registry,
                           mode="send")
    assert decision.decision in ("BLOCK", "HOLD")
    assert decision.sent_text is None


def test_gzip_blob_held_as_entropy(policy, registry):
    """PARTIAL: a gzip+base64 key HOLDs (entropy net), but is not proven.

    Nothing is sent today, which is why this test passes. It stays PARTIAL
    because the hold rests on a generic high-entropy finding: no span names
    the provider key, so a benign judge verdict or a careless approve could
    release the blob still encoded.
    """
    blob = base64.b64encode(gzip.compress(_payload().encode())).decode()
    decision = engine.gate(f"debug log: {blob}", policy, registry,
                           mode="send")
    assert decision.decision == "HOLD"
    assert decision.sent_text is None
    assert any(span.subclass == "entropy" for span in decision.spans)
    assert not any(span.subclass == "provider_key"
                   for span in decision.spans)
