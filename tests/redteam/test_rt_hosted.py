"""Red-team: hosted demo mode keeps nothing (T79).

Attacker's view of ``SF_HOSTED=1``: custom rules must never reach disk, and
the habits endpoints (suggestions that learn from choices) must refuse to
learn from strangers. Uploads stay capped at 1 MB. No network is touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from shadowfenced import app as app_module
from shadowfenced import hosted as hosted_module
from shadowfenced import uploads
from shadowfenced.app import (
    SuggestionApplyRequest,
    apply_suggestion,
    dismiss_suggestion,
    handle_upload,
    list_suggestions,
    undo_suggestion,
)
from shadowfenced.policy import load_custom_rules, reset_hosted_rules
from shadowfenced.rules import CreateRuleRequest, create_rule


@pytest.fixture()
def hosted_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SF_HOSTED", "1")
    monkeypatch.setenv("SF_LOCAL_POLICY", str(tmp_path / "policy.local.yaml"))
    app_module.reset_state()
    reset_hosted_rules()
    uploads.reset_for_tests()
    yield tmp_path
    app_module.reset_state()
    reset_hosted_rules()
    uploads.reset_for_tests()


def test_custom_rules_never_touch_disk(hosted_env):
    """STOPPED: rules live in memory only; the policy file is never born."""
    target = Path(os.environ["SF_LOCAL_POLICY"])
    create_rule(CreateRuleRequest(kind="codename", value="DEMO01",
                                  action="redact"))
    assert len(load_custom_rules()) == 1
    assert not target.exists()


def test_hosted_rules_forgotten_on_restart(hosted_env):
    """STOPPED: a restart (fresh process state) drops every hosted rule."""
    create_rule(CreateRuleRequest(kind="codename", value="DEMO02",
                                  action="redact"))
    assert len(load_custom_rules()) == 1
    reset_hosted_rules()
    app_module.reset_state()
    assert load_custom_rules() == []
    assert not Path(os.environ["SF_LOCAL_POLICY"]).exists()


def test_habits_endpoints_disabled(hosted_env):
    """STOPPED: suggestions list empty; apply/dismiss/undo refuse (403)."""
    payload = list_suggestions()
    assert payload["suggestions"] == []
    assert payload["disabled"] is True
    with pytest.raises(HTTPException) as caught:
        apply_suggestion("whatever", SuggestionApplyRequest())
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:
        dismiss_suggestion("whatever")
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:
        undo_suggestion("whatever")
    assert caught.value.status_code == 403


def test_hosted_upload_cap(hosted_env):
    """STOPPED: files past 1 MB are unscannable, never passed through."""
    big = b"a" * (hosted_module.HOSTED_MAX_FILE_BYTES + 1)
    assert handle_upload("big.txt", big)["status"] == "unscannable"
    exact = b"a" * hosted_module.HOSTED_MAX_FILE_BYTES
    assert handle_upload("exact.txt", exact)["status"] != "unscannable"
