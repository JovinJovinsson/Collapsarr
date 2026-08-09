"""Contract tests for the Scheduled Task registry endpoint (COL-122).

Covers ``GET /api/system/tasks``: the auth gate, the fixed four-row shape
(Library scan, Health checks, Backups, Update check) in that order, each
row's ``interval_label`` matching its scheduler's configured cadence, the
``enable_scheduler=False``/never-run-yet null-``next_run_at`` behaviour the
ticket's acceptance criteria call out explicitly, and that driving each
task's existing manual-trigger endpoint produces a genuine, non-null
``next_run_at`` afterwards.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr.config import Settings
from collapsarr.health import DiskUsage
from collapsarr.main import create_app
from collapsarr.settings.service import get_global_settings

EXPECTED_TASK_NAMES = ["Library scan", "Health checks", "Backups", "Update check"]


class _FakeUsage(NamedTuple):
    total: int
    used: int
    free: int


def _ample_free_space(_path: str) -> DiskUsage:
    """Deterministic 90%-free disk reading (mirrors ``conftest.py``'s helper of the same name)."""
    return _FakeUsage(total=1000, used=100, free=900)


def _offline_update_check_transport() -> httpx.MockTransport:
    """A deterministic, offline stand-in for the GitHub Releases fetch (mirrors ``conftest.py``)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="offline in tests")

    return httpx.MockTransport(handler)


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #
def test_endpoint_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/system/tasks").status_code == 401


# --------------------------------------------------------------------------- #
# Shape: one row per Scheduled Task, in a fixed order
# --------------------------------------------------------------------------- #
def test_lists_the_four_scheduled_tasks_in_order(client: TestClient) -> None:
    headers = _auth_headers(client)

    response = client.get("/api/system/tasks", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert [row["name"] for row in body] == EXPECTED_TASK_NAMES
    for row in body:
        assert set(row) == {
            "name",
            "interval_label",
            "next_run_at",
            "last_run_at",
            "scheduler_enabled",
        }


def test_interval_labels_reflect_each_tasks_configured_cadence(client: TestClient) -> None:
    headers = _auth_headers(client)

    body = client.get("/api/system/tasks", headers=headers).json()
    by_name = {row["name"]: row for row in body}

    # Settings.scan_interval_hours defaults to 6.0.
    assert by_name["Library scan"]["interval_label"] == "Every 6 hours"
    # HealthCheckScheduler.INTERVAL_SECONDS is a fixed 300s (5 min).
    assert by_name["Health checks"]["interval_label"] == "Every 5 minutes"
    # GlobalSettings.backup_interval_days defaults to 7.
    assert by_name["Backups"]["interval_label"] == "Every 7 days"
    # UpdateCheckScheduler.INTERVAL_SECONDS is a fixed 24h.
    assert by_name["Update check"]["interval_label"] == "Every 24 hours"


# --------------------------------------------------------------------------- #
# enable_scheduler=False / never-run-yet: next_run_at is null, scheduler_enabled reflects reality
# --------------------------------------------------------------------------- #
def test_disabled_scheduler_and_never_run_tasks_report_null_next_run(client: TestClient) -> None:
    """The shared ``client`` fixture builds its app with ``enable_scheduler=False``
    (the default): Library Scan and Backups are never wired at all (never run,
    can't run, so both timestamps are null). Health Checks and Update Check
    always run one synchronous tick at startup regardless of the flag, so they
    *do* have a ``last_run_at`` -- but their periodic loop isn't running, so
    ``next_run_at`` must still be null for them too.
    """
    headers = _auth_headers(client)

    body = client.get("/api/system/tasks", headers=headers).json()
    by_name = {row["name"]: row for row in body}

    library_scan = by_name["Library scan"]
    assert library_scan["scheduler_enabled"] is False
    assert library_scan["last_run_at"] is None
    assert library_scan["next_run_at"] is None

    backups = by_name["Backups"]
    assert backups["scheduler_enabled"] is False
    assert backups["last_run_at"] is None
    assert backups["next_run_at"] is None

    health_checks = by_name["Health checks"]
    assert health_checks["scheduler_enabled"] is False
    assert health_checks["last_run_at"] is not None
    assert health_checks["next_run_at"] is None

    update_check = by_name["Update check"]
    assert update_check["scheduler_enabled"] is False
    assert update_check["last_run_at"] is not None
    assert update_check["next_run_at"] is None


# --------------------------------------------------------------------------- #
# enable_scheduler=True: a real run computes a genuine next_run_at
# --------------------------------------------------------------------------- #
def test_enabled_scheduler_reports_true_and_a_real_run_computes_next_run_at(
    settings: Settings,
) -> None:
    app = create_app(
        settings=settings,
        disk_usage=_ample_free_space,
        update_check_transport=_offline_update_check_transport(),
        enable_scheduler=True,
    )
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        # Drive each task's own existing manual-trigger endpoint once, so
        # every one of the four has a real last_run_at to compute from --
        # avoids racing the background threads' own periodic first tick.
        assert test_client.post("/api/jobs/scan", headers=headers).status_code == 202
        assert (
            test_client.post("/api/system/health-checks/recheck", headers=headers).status_code
            == 200
        )
        assert test_client.post("/api/system/backup", headers=headers).status_code == 202
        assert test_client.post("/api/system/updates/recheck", headers=headers).status_code == 200

        body = test_client.get("/api/system/tasks", headers=headers).json()
        by_name = {row["name"]: row for row in body}

        for name in EXPECTED_TASK_NAMES:
            row = by_name[name]
            assert row["scheduler_enabled"] is True, name
            assert row["last_run_at"] is not None, name
            assert row["next_run_at"] is not None, name
            last_run = datetime.fromisoformat(row["last_run_at"])
            next_run = datetime.fromisoformat(row["next_run_at"])
            assert next_run > last_run, name
