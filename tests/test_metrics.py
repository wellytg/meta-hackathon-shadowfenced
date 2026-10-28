"""Placeholder: metrics harness scores a corpus run."""

import pytest

from tripwire import metrics


@pytest.mark.xfail(reason="metrics not implemented (scaffold only)", strict=True)
def test_run_corpus_reports_zero_leaks():
    report = metrics.run_corpus("corpus/smuggling.jsonl")
    assert report.canaries_leaked == 0
