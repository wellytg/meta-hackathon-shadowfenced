"""Placeholder: ECS log appends a chained event and verifies."""

import pytest

from shadowfenced import ecs_log


@pytest.mark.xfail(reason="ecs_log not implemented (scaffold only)", strict=True)
def test_append_then_verify_chain(tmp_path):
    log = str(tmp_path / "decisions.jsonl")
    ecs_log.append_event(log, ecs_log.DecisionEvent(
        action="gate.block.deterministic", decision="block", proof_status="deterministic"))
    assert ecs_log.verify_chain(log) is True
