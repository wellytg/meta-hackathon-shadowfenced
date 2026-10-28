"""Placeholder: local bare remote initializes on disk."""

import pytest

from tripwire import remotes


@pytest.mark.xfail(reason="remotes not implemented (scaffold only)", strict=True)
def test_init_bare_remote(tmp_path):
    path = remotes.init_bare_remote(str(tmp_path / "demo.git"))
    assert path.endswith("demo.git")
