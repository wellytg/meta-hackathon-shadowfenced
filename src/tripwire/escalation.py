"""Escalation and resume (Agents 101 pattern).

Pauses the agent run when the verifier reports an unproven suspicion, builds
an evidence card for a human, records the approve/deny ruling, and resumes
the run from the held call.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvidenceCard:
    """Everything a human needs to rule on one held action."""

    tool_name: str
    arguments: dict
    detector_hits: list = field(default_factory=list)
    verdict: object | None = None
    proof: object | None = None


@dataclass
class Ruling:
    """A human's decision on an evidence card."""

    approved: bool
    reason: str = ""


def build_card(
    tool_name: str, arguments: dict, detector_hits: list, verdict: object, proof: object
) -> EvidenceCard:
    """Assemble the evidence card for one held action."""
    raise NotImplementedError


def rule_on_card(card: EvidenceCard, approved: bool, reason: str = "") -> Ruling:
    """Record a human ruling on an evidence card."""
    raise NotImplementedError


def resume_after_ruling(card: EvidenceCard, ruling: Ruling) -> str:
    """Resume the run: execute the held call or skip it per the ruling."""
    raise NotImplementedError
