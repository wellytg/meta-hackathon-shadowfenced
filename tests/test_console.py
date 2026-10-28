"""Placeholder: console renders one gated turn as text."""

import pytest

from tripwire import console


@pytest.mark.xfail(reason="console not implemented (scaffold only)", strict=True)
def test_render_turn_mentions_decision():
    text = console.render_turn({"tool": "git_commit", "decision": "block"})
    assert "block" in text
