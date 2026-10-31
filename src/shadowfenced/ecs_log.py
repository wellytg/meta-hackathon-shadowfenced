"""The ECS hash-chained log.

Appends every gate decision as one ECS event (event.* plus beta gen_ai.*)
to a JSONL file, hash-chained to the previous event with sha256 so the demo
log is tamper-evident and ready for Elastic/Wazuh ingest.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DecisionEvent:
    """One gate decision ready to serialize as ECS JSON."""

    action: str
    decision: str
    proof_status: str
    tool_name: str = ""
    canary_id: str = ""


def hash_event(canonical_json: str, prev_hash: str) -> str:
    """Chain one event to its predecessor with sha256."""
    raise NotImplementedError


def append_event(log_path: str, event: DecisionEvent) -> dict:
    """Serialize, hash-chain, and append one event; return the record."""
    raise NotImplementedError


def verify_chain(log_path: str) -> bool:
    """Recompute every hash and return True when the chain is intact."""
    raise NotImplementedError
