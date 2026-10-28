"""The target coding agent's tools.

Defines git_commit, create_branch, open_issue, fetch_url, and write_file in
the flat Responses API schema ({"type": "function", "name": ..., 
"parameters": ...}) and executes approved calls against the local targets.
Tool results go back as {"type": "function_call_output", "call_id": ...,
"output": ...}.
"""

from __future__ import annotations

TOOL_NAMES: tuple[str, ...] = (
    "git_commit",
    "create_branch",
    "open_issue",
    "fetch_url",
    "write_file",
)


def tool_definitions() -> list[dict]:
    """Return the flat Responses API tool definitions for all five tools."""
    raise NotImplementedError


def execute_tool(tool_name: str, arguments: dict) -> dict:
    """Execute one approved tool call and return a function_call_output."""
    raise NotImplementedError


def format_call_output(call_id: str, output: str) -> dict:
    """Wrap a tool result as a function_call_output message."""
    raise NotImplementedError
