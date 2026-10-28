"""Placeholder: verifier proves a decoding against a canary."""

import pytest

from tripwire import verifier


@pytest.mark.xfail(reason="verifier not implemented (scaffold only)", strict=True)
def test_verify_verdict_proves_base64():
    proof = verifier.verify_verdict(object(), {"k": "TRIPWIRE-CANARY-AKIA0000FAKE0001"})
    assert proof.status == "proven"
