"""The metrics harness.

Runs the smuggling-technique corpus (~30 cases, 8-10 techniques) and benign
agent runs through the gate, then reports catch rate, false-block rate,
canaries leaked (target 0), latency per gated action (local vs Modal), and
Glimmer lift: catches the deterministic layer alone missed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MetricsReport:
    """Scores from one corpus run."""

    catch_rate: float
    false_block_rate: float
    canaries_leaked: int
    mean_latency_s: float
    glimmer_lift: int


def run_corpus(corpus_path: str) -> MetricsReport:
    """Run every corpus case through the gate and score the outcomes."""
    raise NotImplementedError


def run_benign(scenarios_path: str) -> float:
    """Run benign scenarios and return the false-block rate."""
    raise NotImplementedError


def print_scoreboard(report: MetricsReport) -> None:
    """Print the demo scoreboard from a metrics report."""
    raise NotImplementedError
