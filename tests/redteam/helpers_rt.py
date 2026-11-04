"""Shared red-team helpers: offline only, synthetic data only.

Every provider key shape is built at runtime from fragments, exactly like
``tests/test_t47_provider_keys.py``, so ``tests/test_repo_guard.py`` stays
green. No test here touches the network: upstream answers through an httpx
``MockTransport`` and Tier 2 stays off (see ``tests/conftest.py``).

Plain helpers only: each test file declares its own tiny fixtures locally
(``policy``, ``registry``, ``mock_upstream``), following the repo convention.
A ``conftest.py`` in this directory would shadow ``tests/conftest.py`` for
``from conftest import ...`` users, so there is deliberately none here.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from shadowfenced.app import app
from shadowfenced.canary import CanaryRegistry
from shadowfenced.policy import load_policy

ROOT = Path(__file__).resolve().parents[2]

REGISTRY_SEED = "redteam-seed"
REGISTRY_KINDS = ("db_password", "api_key")


def frag(*parts: str) -> str:
    """Join key-shape fragments at runtime (never one literal)."""
    return "".join(parts)


def aws_key() -> str:
    """Fake AWS access key ID: fixed body, real shape."""
    return frag("AK", "IA") + "X" * 16


def openai_key() -> str:
    """Fake OpenAI legacy key: fixed body, real shape."""
    return frag("s", "k-") + "B" * 48


def email() -> str:
    """Fake email on a reserved TLD (never a real mailbox)."""
    return "ada.lovelace@mail.example"


def make_policy():
    """The repo policy (fresh per test, like the other test files)."""
    return load_policy(ROOT / "policy.yaml")


def make_registry():
    """A fixed-seed canary registry for red-team tests."""
    return CanaryRegistry(REGISTRY_SEED, REGISTRY_KINDS)


def upstream_mock():
    """Yield a ``make(answer)`` upstream stub; unhooks itself after."""
    calls: list[httpx.Request] = []

    def make(answer: str = "happy to help", status: int = 200) -> list:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                status,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": answer}}
                    ]
                },
            )

        app.state.upstream_transport = httpx.MockTransport(handler)
        return calls

    yield make
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")
