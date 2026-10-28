"""The local git remote and the mock issue tracker.

All exfiltration targets are local only: a bare git repository on disk and a
FastAPI mock issue tracker. Canaries never touch real GitHub, since a public
repo would expose even fake secrets and trip secret scanning.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RemoteConfig:
    """Locations of the local targets."""

    git_remote_path: str
    tracker_base_url: str


def init_bare_remote(path: str) -> str:
    """Create a local bare git remote at path."""
    raise NotImplementedError


def create_tracker_app() -> object:
    """Build the FastAPI mock issue tracker application."""
    raise NotImplementedError


def open_issue(title: str, body: str) -> dict:
    """File an issue against the mock tracker."""
    raise NotImplementedError
