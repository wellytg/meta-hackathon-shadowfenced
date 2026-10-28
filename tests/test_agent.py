"""Placeholder: target agent runs a task through the gated tool loop."""

import pytest

from tripwire import agent


@pytest.mark.skip(reason="agent needs a live Model API endpoint (scaffold only)")
def test_run_task_completes():
    config = agent.AgentConfig(api_key="x", base_url="https://api.meta.ai/v1",
                               model="muse-spark-1.3")
    result = agent.run_task(config, "fix the bug", [])
    assert result.completed_task is True
