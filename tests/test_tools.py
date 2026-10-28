"""Placeholder: tools expose the flat Responses API schema."""

import pytest

from tripwire import tools


@pytest.mark.xfail(reason="tools not implemented (scaffold only)", strict=True)
def test_tool_definitions_use_flat_schema():
    defs = tools.tool_definitions()
    names = {d["name"] for d in defs}
    assert names == {"git_commit", "create_branch", "open_issue", "fetch_url", "write_file"}
    assert all(d["type"] == "function" for d in defs)
