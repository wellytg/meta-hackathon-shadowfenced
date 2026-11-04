"""Red-team: invisible characters and look-alike letters (T79).

Tier 1 matches plain-text patterns. It does not normalize Unicode first.
A zero-width joiner inside a key, or a Cyrillic "a" inside an email, breaks
the pattern while a human (or a model) still reads the value.

Carrier sentences avoid sensitivity cue words on purpose: with a cue word
the message HOLDs for review, which would hide the detector gap. These tests
pin the detector behavior itself.
"""

from __future__ import annotations

import pytest

from shadowfenced import engine
from helpers_rt import aws_key, email, openai_key, make_policy, make_registry


@pytest.fixture()
def policy():
    return make_policy()


@pytest.fixture()
def registry():
    return make_registry()


def test_plain_key_blocked_control(policy, registry):
    """Control: the same key in plain ASCII is BLOCKed."""
    key = aws_key()
    decision = engine.gate(f"deploy with {key} tonight", policy, registry,
                           mode="send")
    assert decision.decision == "BLOCK"
    assert decision.sent_text is None


def test_plain_email_redacted_control(policy, registry):
    """Control: the same email in plain ASCII is redacted, never sent raw."""
    mail = email()
    decision = engine.gate(f"write to {mail} about the rota", policy,
                           registry, mode="send")
    assert decision.decision == "REDACT"
    assert decision.sent_text is not None
    assert mail not in decision.sent_text


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: Tier 1 does not strip zero-width characters",
)
def test_zero_width_split_key_stopped(policy, registry):
    """A key laced with zero-width spaces must not pass as clean."""
    key = aws_key()
    smuggled = "​".join(key)  # U+200B zero-width space between every char
    decision = engine.gate(f"deploy with {smuggled} tonight", policy,
                           registry, mode="send")
    assert decision.decision in ("BLOCK", "HOLD")
    assert any(span.cls == "secret" for span in decision.spans)


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: Tier 1 does not strip zero-width characters",
)
def test_zero_width_split_email_stopped(policy, registry):
    """An email split by a zero-width joiner must not pass as clean."""
    mail = email()
    smuggled = mail.replace("@", "‌@")
    decision = engine.gate(f"write to {smuggled} about the rota", policy,
                           registry, mode="send")
    assert decision.decision in ("REDACT", "BLOCK", "HOLD")
    assert decision.sent_text is None or mail not in decision.sent_text


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: Tier 1 does not fold homoglyphs to ASCII",
)
def test_homoglyph_key_stopped(policy, registry):
    """A key starting with full-width A K must not pass as clean."""
    key = "ＡＫ" + aws_key()[2:]
    decision = engine.gate(f"deploy with {key} tonight", policy, registry,
                           mode="send")
    assert decision.decision in ("BLOCK", "HOLD")
    assert any(span.cls == "secret" for span in decision.spans)


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: Tier 1 does not fold homoglyphs to ASCII",
)
def test_homoglyph_email_stopped(policy, registry):
    """An email with Cyrillic look-alike letters must not pass as clean."""
    mail = email().replace("a", "а")
    assert mail != email()  # the swap really happened (Cyrillic U+0430)
    decision = engine.gate(f"write to {mail} about the rota", policy,
                           registry, mode="send")
    assert decision.decision in ("REDACT", "BLOCK", "HOLD")
    assert decision.sent_text is None or mail not in decision.sent_text


def test_openai_key_shape_control(policy, registry):
    """Control: a second provider shape is BLOCKed in plain ASCII."""
    key = openai_key()
    decision = engine.gate(f"deploy with {key} tonight", policy, registry,
                           mode="send")
    assert decision.decision == "BLOCK"
    assert decision.sent_text is None
