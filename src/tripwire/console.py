"""Console UI placeholder.

Will become the workshop-style turn-by-turn demo console: plain leak,
encoded leak, acrostic catch with evidence card, ambiguous escalation with
human deny, clean finish, then the scoreboard. For now it only proves the
console entry point exists.
"""

from __future__ import annotations


def render_turn(turn: dict) -> str:
    """Render one gated turn as console text."""
    raise NotImplementedError


def render_evidence_card(card: object) -> str:
    """Render an escalation evidence card as console text."""
    raise NotImplementedError


def main() -> None:
    """Console entry point (tripwire command)."""
    raise NotImplementedError
