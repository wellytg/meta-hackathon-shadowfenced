"""The Glimmer watcher client.

Sends a held outbound action plus the recent trace window to the Glimmer
open-weights endpoint (local llama.cpp server or Modal vLLM, both
OpenAI-compatible) and returns a validated strict-JSON verdict: suspicious or
clean, the cited span, and the proposed decoding. Never returns raw text.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WatcherVerdict:
    """Validated verdict for one held action."""

    verdict: str  # "suspicious" | "clean"
    technique: str
    span_field: str
    span_start: int
    span_end: int
    span_text: str
    decoding_method: str
    decoding_result: str
    confidence: float
    rationale: str = ""
    latency_s: float = 0.0


@dataclass
class WatcherConfig:
    """Endpoint settings loaded from the environment."""

    base_url: str
    model: str
    timeout_s: float = 30.0
    extra: dict = field(default_factory=dict)


def load_config_from_env() -> WatcherConfig:
    """Build endpoint config from GLIMMER_BASE_URL and GLIMMER_MODEL."""
    raise NotImplementedError


def build_prompt(tool_name: str, arguments: dict, trace: list[dict]) -> list[dict]:
    """Build the chat messages asking for a strict-JSON verdict."""
    raise NotImplementedError


def judge_action(
    config: WatcherConfig, tool_name: str, arguments: dict, trace: list[dict]
) -> WatcherVerdict:
    """Send one held action to Glimmer and return the validated verdict."""
    raise NotImplementedError
