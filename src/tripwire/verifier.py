"""The verifier: model proposes, code verifies.

Re-runs Glimmer's proposed decoding on the cited span in plain code and
compares the result against the seeded canaries. A match is a proven catch
(block). A suspicious verdict that cannot be proven escalates to a human.
The verifier never calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Proof:
    """Result of re-running a proposed decoding in code."""

    status: str  # "deterministic" | "proven" | "unproven" | "clean"
    decoded: str = ""
    canary_id: str = ""


def apply_decoding(method: str, span_text: str, steps: list[str]) -> str:
    """Apply one named decoding method to the cited span text."""
    raise NotImplementedError


def verify_verdict(verdict: object, canaries: dict[str, str]) -> Proof:
    """Re-run the verdict's decoding and compare against the canaries."""
    raise NotImplementedError
