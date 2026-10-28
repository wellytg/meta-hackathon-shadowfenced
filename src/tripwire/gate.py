"""The gate and the role manifest.

The gate holds every outbound tool call before it runs. The role manifest
states which tools each agent role may use and within what capability limits.
The gate will consult the deterministic detector, the Glimmer watcher, and the
verifier (in that order) before allowing, blocking, or escalating a call.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RoleManifest:
    """Capability limits for one agent role."""

    role: str
    allowed_tools: list[str] = field(default_factory=list)
    max_arg_bytes: int = 4096


@dataclass
class GateDecision:
    """Outcome of holding one outbound tool call."""

    action: str
    decision: str  # "allow" | "block" | "escalate"
    reason: str = ""


def load_manifest(path: str) -> RoleManifest:
    """Load a role manifest from a file."""
    raise NotImplementedError


def check_tool_allowed(manifest: RoleManifest, tool_name: str) -> bool:
    """Return True when this role may call this tool."""
    raise NotImplementedError


def hold_action(manifest: RoleManifest, tool_name: str, arguments: dict) -> GateDecision:
    """Hold one outbound tool call and return allow, block, or escalate."""
    raise NotImplementedError
