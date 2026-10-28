"""Placeholder: escalation pauses the run and resumes after a ruling."""

import pytest

from tripwire import escalation


@pytest.mark.xfail(reason="escalation not implemented (scaffold only)", strict=True)
def test_resume_after_deny_skips_call():
    card = escalation.EvidenceCard(tool_name="git_commit", arguments={})
    ruling = escalation.rule_on_card(card, approved=False, reason="contains a canary")
    assert escalation.resume_after_ruling(card, ruling) == "skipped"
