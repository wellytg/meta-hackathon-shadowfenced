"""Placeholder: detector finds a base64 canary in a branch name."""

import pytest

from tripwire import detectors


@pytest.mark.xfail(reason="detectors not implemented (scaffold only)", strict=True)
def test_scan_action_finds_base64_canary():
    hits = detectors.scan_action(
        {"branch": "backup-VFJJUFdJUkUtQ0FOQVJZLUFLSUE="},
        {"api_key": "TRIPWIRE-CANARY-AKIA0000FAKE0001"},
    )
    assert any(h.encoding == "base64" for h in hits)
