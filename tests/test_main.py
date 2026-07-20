"""Tests for the application factory's scheduler wiring (COL-22).

The default ``create_app`` path stays lightweight (log-only stub hook, no
background thread); ``enable_scheduler=True`` is the production opt-in that
swaps in the real :class:`~collapsarr.jobs.scheduler.JobScheduler`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from collapsarr.arr.webhooks import default_on_file_ready_hook
from collapsarr.config import Settings
from collapsarr.jobs.scheduler import JobScheduler
from collapsarr.main import create_app


def test_default_app_uses_the_stub_hook_and_no_scheduler(settings: Settings) -> None:
    app = create_app(settings=settings)
    with TestClient(app):
        state = app.state
        assert state.on_file_ready is default_on_file_ready_hook
        assert not hasattr(state, "job_scheduler")


def test_enable_scheduler_wires_the_real_hook_and_starts_a_scheduler(
    settings: Settings,
) -> None:
    app = create_app(settings=settings, enable_scheduler=True)
    with TestClient(app):
        state = app.state
        assert isinstance(state.job_scheduler, JobScheduler)
        # The webhook's "file ready" hook is now the scheduler's enqueue path.
        assert state.on_file_ready == state.job_scheduler.on_file_ready


def test_explicit_hook_overrides_the_scheduler(settings: Settings) -> None:
    captured: list[object] = []
    app = create_app(
        settings=settings, on_file_ready=captured.append, enable_scheduler=True
    )
    with TestClient(app):
        state = app.state
        assert state.on_file_ready == captured.append
        assert not hasattr(state, "job_scheduler")
