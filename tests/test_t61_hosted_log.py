"""T61 hosted log: project-root resolve, visible write failures, modal path."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from shadowfenced import ecs_log
from shadowfenced import health as health_module
from shadowfenced.app import SendRequest, app, send


def _project_root() -> Path:
    return Path(ecs_log.__file__).resolve().parents[2]


def _clear_log_error():
    ecs_log.clear_last_write_error()


def test_relative_resolves_against_project_root(monkeypatch, tmp_path):
    """Relative log paths resolve against the repo root, not the CWD."""
    _clear_log_error()
    try:
        monkeypatch.delenv("SF_LOG_PATH", raising=False)
        monkeypatch.chdir(tmp_path)
        expected = str(_project_root() / "runs" / "decisions.jsonl")
        assert ecs_log.resolve_path("runs/decisions.jsonl") == expected
        monkeypatch.setenv("SF_LOG_PATH", "runs/decisions.jsonl")
        assert ecs_log.resolve_path() == expected
        assert ecs_log.resolve_path(None) == expected
    finally:
        _clear_log_error()


def test_absolute_path_passes_through(monkeypatch, tmp_path):
    """Absolute overrides (explicit or env) are never joined to the root."""
    _clear_log_error()
    try:
        target = str(tmp_path / "decisions.jsonl")
        monkeypatch.setenv("SF_LOG_PATH", target)
        assert ecs_log.resolve_path() == target
        assert ecs_log.resolve_path(target) == target
    finally:
        _clear_log_error()


def test_write_failure_degraded_and_send_still_works(monkeypatch, tmp_path, caplog):
    """A bad log location degrades health but never fails the gate."""
    _clear_log_error()
    try:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        bad_path = str(blocker / "decisions.jsonl")
        monkeypatch.setenv("SF_LOG_PATH", bad_path)

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )

        app.state.upstream_transport = httpx.MockTransport(_handler)
        try:
            with caplog.at_level(logging.WARNING, logger="shadowfenced.ecs_log"):
                body = send(
                    SendRequest(messages=[{"role": "user", "content": "hello there"}])
                )
        finally:
            if hasattr(app.state, "upstream_transport"):
                delattr(app.state, "upstream_transport")
        assert body["decision"] == "ALLOW"
        assert ecs_log.get_last_write_error() is not None
        row = health_module.check_log()
        assert row["name"] == "log"
        assert row["status"] == "degraded"
        assert row["detail"] == (
            f"log not writable: {ecs_log.get_last_write_error()}"
        )
        warnings = [
            record
            for record in caplog.records
            if record.name == "shadowfenced.ecs_log"
            and "not writable" in record.getMessage()
        ]
        assert warnings, "expected one log-not-writable warning"
        for record in warnings:
            assert "hello there" not in record.getMessage()
    finally:
        _clear_log_error()
        if hasattr(app.state, "upstream_transport"):
            try:
                delattr(app.state, "upstream_transport")
            except AttributeError:
                pass


def test_modal_app_sets_writable_log_path():
    """The hosted entrypoint must pin an absolute, writable log path."""
    text = (Path(__file__).resolve().parents[1] / "deploy" / "modal_app.py").read_text(
        encoding="utf-8"
    )
    assert 'os.environ.setdefault("SF_LOG_PATH", "/tmp/shadowfenced/decisions.jsonl")' in text
    assert text.index("SF_LOG_PATH") < text.index("from shadowfenced.app import")
