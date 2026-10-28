"""The target coding agent.

Drives the muse-spark-1.3 loop: given a normal coding task, it issues tool
calls through the gate. A planted issue or README may carry a hidden
prompt-injection telling it to exfiltrate the canaries. Uses the OpenAI
Python SDK with base_url from .env and the flat Responses API tool schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """Model connection settings loaded from the environment."""

    api_key: str
    base_url: str
    model: str


@dataclass
class AgentResult:
    """Outcome of one agent run."""

    completed_task: bool
    tool_calls: list[dict] = field(default_factory=list)


def load_config_from_env() -> AgentConfig:
    """Build agent config from MODEL_API_KEY/BASE_URL/MODEL."""
    raise NotImplementedError


def run_task(config: AgentConfig, task: str, tools: list[dict]) -> AgentResult:
    """Run one coding task through the gated tool loop."""
    raise NotImplementedError
