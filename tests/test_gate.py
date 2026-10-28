"""Placeholder: gate holds an outbound call per the role manifest."""

import pytest

from tripwire import gate


@pytest.mark.xfail(reason="gate not implemented (scaffold only)", strict=True)
def test_hold_action_blocks_disallowed_tool():
    manifest = gate.RoleManifest(role="coder", allowed_tools=["write_file"])
    decision = gate.hold_action(manifest, "git_commit", {"message": "hi"})
    assert decision.decision == "block"
