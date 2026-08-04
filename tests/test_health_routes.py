"""Contract tests for the health-check detail REST endpoints (COL-76, COL-82, COL-83).

Covers ``GET /api/system/health-checks``: the API-gate behaviour (401 without
a key/session), the full per-row shape (code, category, severity, status,
message, instance_id, first/last-checked timing, dismissed_at), and that it
works generically for any number/kind of registered check with mixed
severities/statuses -- not hardcoded to FFmpeg.

Also covers the COL-82 ``POST .../{id}/dismiss`` and ``.../{id}/undismiss``
actions: success shape, the auth gate, the 404 (unknown id) and 409 (not
currently failing) refusals, and that a dismiss actually removes the row from
the unauthenticated ``/health`` banner while it stays visible -- marked
dismissed -- on this authenticated list endpoint.

Also covers the COL-83 ``POST .../recheck`` action: the auth gate, that it
runs every registered check immediately and returns the resulting full state,
that a manual recheck updates persisted state and fires the same
edge-triggered pass<->fail notification a scheduled tick would (reusing
``reconcile_health_results``'s existing transition-detection path -- no
separate notification mechanism), and that it never disturbs the background
scheduler's own periodic thread/timer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.health import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    DiskUsage,
    FfmpegCheckResult,
    HealthCheckResult,
)
from collapsarr.health.service import reconcile_health_results
from collapsarr.main import create_app
from collapsarr.notify.service import update_notifier_config
from collapsarr.settings.service import get_global_settings


class _FakeUsage(NamedTuple):
    total: int
    used: int
    free: int


def _ample_free_space() -> Callable[[str], DiskUsage]:
    """A disk-usage probe reporting 90% free (mirrors ``test_health.py``'s
    helper of the same name) -- keeps the COL-83 tests below, which build
    their own ``create_app`` to control the FFmpeg checker, from also
    reflecting this host's real disk-space reading.
    """
    usage = _FakeUsage(total=1000, used=100, free=900)
    return lambda _path: usage


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _seed(client: TestClient, results: list[HealthCheckResult]) -> None:
    """Reconcile ``results`` against the live app's database (COL-75's service)."""
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        assert isinstance(session, Session)
        reconcile_health_results(session, results, now=lambda: datetime(2026, 1, 1, tzinfo=UTC))


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #
def test_endpoint_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/system/health-checks").status_code == 401


# --------------------------------------------------------------------------- #
# Shape + generic multi-check, mixed-severity coverage
# --------------------------------------------------------------------------- #
def test_lists_every_check_with_full_detail_and_mixed_severities(client: TestClient) -> None:
    headers = _auth_headers(client)

    # Three Check Keys, none of them FFmpeg -- proves the endpoint is generic
    # over whatever is registered/persisted, not hardcoded to one check. Uses
    # "WARN-DISK-999" (not "WARN-DISK-001") for the synthetic disk-category
    # example so it doesn't collide with the real disk-space check's own Check
    # Code (COL-79, registered by default -- see the count comment below).
    # "WARN-JOBS-001" *does* match the real failed-jobs check's own Check Code
    # (COL-81, also registered by default) -- deliberately, to prove seeding a
    # passing result for an already-persisted real check's Check Key updates
    # that same row rather than adding a second one (see the count comment).
    _seed(
        client,
        [
            HealthCheckResult.failed(
                code="ERR-CONN-001",
                category="connectivity",
                severity=SEVERITY_ERROR,
                message="Sonarr instance unreachable.",
                instance_id=1,
            ),
            HealthCheckResult.failed(
                code="WARN-DISK-999",
                category="disk",
                severity=SEVERITY_WARNING,
                message="Disk space low.",
            ),
            HealthCheckResult.ok(
                code="WARN-JOBS-001",
                category="jobs",
                severity=SEVERITY_WARNING,
                message="Job failure rate nominal.",
            ),
        ],
    )

    response = client.get("/api/system/health-checks", headers=headers)
    assert response.status_code == 200
    body = response.json()
    # 8, not 6 + 3: the shared `client` fixture's app already ran its own
    # startup tick (test_health_check.py, test_health_arr_instances.py,
    # test_health_disk_space.py, test_health_database_writable.py), persisting
    # a passing FFmpeg row, a failing no-Arr-instances (COL-77) row, a passing
    # disk-space warning/error pair (COL-79, the fixture pins disk usage to
    # 90% free), a passing database-writable row (COL-80), and a passing
    # failed-jobs row (COL-81, a fresh job-history table has zero failures) --
    # 6 rows. Seeding above adds ERR-CONN-001 and WARN-DISK-999 (2 genuinely
    # new rows) and re-seeds WARN-JOBS-001 (updates the existing COL-81 row in
    # place rather than adding a 7th) -- 8 total, proving the endpoint lists
    # every check, seeded ones alongside the real ones.
    assert len(body) == 8
    assert "ffmpeg_missing" in {row["code"] for row in body}

    # Ordered by code then instance_id (service.list_health_check_states).
    codes = [row["code"] for row in body]
    assert codes == sorted(codes)

    by_code = {row["code"]: row for row in body}

    connectivity = by_code["ERR-CONN-001"]
    assert connectivity["category"] == "connectivity"
    assert connectivity["severity"] == "error"
    assert connectivity["status"] == "failing"
    assert connectivity["message"] == "Sonarr instance unreachable."
    assert connectivity["instance_id"] == 1
    assert connectivity["first_failed_at"] is not None
    assert connectivity["last_checked_at"] is not None
    assert "id" in connectivity

    disk = by_code["WARN-DISK-999"]
    assert disk["severity"] == "warning"
    assert disk["status"] == "failing"
    assert disk["instance_id"] is None
    assert disk["first_failed_at"] is not None

    jobs = by_code["WARN-JOBS-001"]
    assert jobs["severity"] == "warning"
    assert jobs["status"] == "passing"
    assert jobs["first_failed_at"] is None


def test_returns_the_startup_ffmpeg_check_with_no_other_checks_seeded(client: TestClient) -> None:
    # The shared `client` fixture's app runs the real startup tick: FFmpeg is
    # expected present in dev/CI (see test_health_check.py), no Arr instances
    # are configured (a fresh settings/database fixture), the fixture pins
    # disk usage to 90% free (COL-79), the database-writable check (COL-80)
    # always succeeds against the fixture's real, writable temp SQLite
    # database, and the failed-jobs check (COL-81) always finds an empty,
    # fresh job-history table -- so exactly six rows are persisted before any
    # test seeds anything else: a passing FFmpeg row, a failing
    # no-Arr-instances (COL-77) row, a passing disk-space warning/error pair
    # (COL-79), a passing database-writable row (COL-80), and a passing
    # failed-jobs row (COL-81).
    headers = _auth_headers(client)
    response = client.get("/api/system/health-checks", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 6
    by_code = {row["code"]: row for row in body}
    assert by_code["ffmpeg_missing"]["status"] == "passing"
    assert by_code["WARN-ARR-001"]["status"] == "failing"
    assert by_code["WARN-DISK-001"]["status"] == "passing"
    assert by_code["ERR-DISK-001"]["status"] == "passing"
    assert by_code["ERR-DB-001"]["status"] == "passing"
    assert by_code["WARN-JOBS-001"]["status"] == "passing"


# --------------------------------------------------------------------------- #
# COL-82: dismiss / undismiss
# --------------------------------------------------------------------------- #


def _seeded_id(client: TestClient, code: str) -> int:
    """Reconciles one failing Check Key and returns its persisted row id."""
    _seed(
        client,
        [
            HealthCheckResult.failed(
                code=code, category="test", severity=SEVERITY_ERROR, message="down"
            )
        ],
    )
    headers = _auth_headers(client)
    body = client.get("/api/system/health-checks", headers=headers).json()
    return int(next(row["id"] for row in body if row["code"] == code))


def test_dismiss_and_undismiss_endpoints_require_authentication(client: TestClient) -> None:
    check_id = _seeded_id(client, "ERR-DISMISS-AUTH")

    assert client.post(f"/api/system/health-checks/{check_id}/dismiss").status_code == 401
    assert client.post(f"/api/system/health-checks/{check_id}/undismiss").status_code == 401


def test_dismiss_returns_404_for_an_unknown_id(client: TestClient) -> None:
    headers = _auth_headers(client)

    response = client.post("/api/system/health-checks/999999/dismiss", headers=headers)

    assert response.status_code == 404


def test_undismiss_returns_404_for_an_unknown_id(client: TestClient) -> None:
    headers = _auth_headers(client)

    response = client.post("/api/system/health-checks/999999/undismiss", headers=headers)

    assert response.status_code == 404


def test_dismiss_returns_409_for_a_currently_passing_check(client: TestClient) -> None:
    headers = _auth_headers(client)
    _seed(
        client,
        [
            HealthCheckResult.ok(
                code="WARN-DISMISS-PASS", category="test", severity=SEVERITY_WARNING, message="ok"
            )
        ],
    )
    check_id = next(
        row["id"]
        for row in client.get("/api/system/health-checks", headers=headers).json()
        if row["code"] == "WARN-DISMISS-PASS"
    )

    response = client.post(f"/api/system/health-checks/{check_id}/dismiss", headers=headers)

    assert response.status_code == 409


def test_dismiss_hides_the_check_from_health_but_marks_it_dismissed_on_the_list(
    client: TestClient,
) -> None:
    headers = _auth_headers(client)
    check_id = _seeded_id(client, "ERR-DISMISS-HIDE")

    dismiss_response = client.post(f"/api/system/health-checks/{check_id}/dismiss", headers=headers)
    assert dismiss_response.status_code == 200
    dismissed_row = dismiss_response.json()
    assert dismissed_row["dismissed_at"] is not None

    # Gone from the unauthenticated /health banner...
    health = client.get("/health").json()
    assert "ERR-DISMISS-HIDE" not in {w["code"] for w in health["warnings"]}

    # ...but still present, marked dismissed, on the full list.
    body = client.get("/api/system/health-checks", headers=headers).json()
    row = next(r for r in body if r["code"] == "ERR-DISMISS-HIDE")
    assert row["status"] == "failing"
    assert row["dismissed_at"] is not None


def test_undismiss_restores_the_check_to_the_health_banner(client: TestClient) -> None:
    headers = _auth_headers(client)
    check_id = _seeded_id(client, "ERR-DISMISS-RESTORE")
    client.post(f"/api/system/health-checks/{check_id}/dismiss", headers=headers)
    assert "ERR-DISMISS-RESTORE" not in {
        w["code"] for w in client.get("/health").json()["warnings"]
    }

    response = client.post(f"/api/system/health-checks/{check_id}/undismiss", headers=headers)

    assert response.status_code == 200
    assert response.json()["dismissed_at"] is None
    health = client.get("/health").json()
    assert "ERR-DISMISS-RESTORE" in {w["code"] for w in health["warnings"]}


# --------------------------------------------------------------------------- #
# COL-83: manual recheck
# --------------------------------------------------------------------------- #


class _ToggleFfmpegChecker:
    """A controllable ``ffmpeg_checker`` so a recheck can flip pass<->fail
    deterministically, without touching the real ``ffmpeg`` binary."""

    def __init__(self, *, available: bool) -> None:
        self.available = available

    def __call__(self) -> FfmpegCheckResult:
        if self.available:
            return FfmpegCheckResult(
                available=True, ffmpeg_path="ffmpeg", detail="FFmpeg found at '/usr/bin/ffmpeg'."
            )
        return FfmpegCheckResult(
            available=False,
            ffmpeg_path="ffmpeg",
            detail="FFmpeg executable 'ffmpeg' was not found.",
        )


def _capture_transport() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    return httpx.MockTransport(handler), seen


def test_recheck_endpoint_requires_authentication(client: TestClient) -> None:
    assert client.post("/api/system/health-checks/recheck").status_code == 401


def test_recheck_runs_every_check_immediately_and_returns_the_full_state(
    settings: Settings,
) -> None:
    checker = _ToggleFfmpegChecker(available=True)
    app = create_app(settings=settings, disk_usage=_ample_free_space(), ffmpeg_checker=checker)
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.post("/api/system/health-checks/recheck", headers=headers)

        assert response.status_code == 200
        body = response.json()
        # Same six default-registered checks the app's own startup tick
        # persists (see test_returns_the_startup_ffmpeg_check_with_no_other_
        # checks_seeded above) -- proves this is a real, full tick, not a
        # stub.
        assert len(body) == 6
        by_code = {row["code"]: row for row in body}
        assert by_code["ffmpeg_missing"]["status"] == "passing"

        # And it's live, not cached: GET reflects the same, freshly persisted rows.
        listed = test_client.get("/api/system/health-checks", headers=headers).json()
        assert {row["code"] for row in listed} == set(by_code)


def test_recheck_updates_persisted_state_and_fires_notification_on_change(
    settings: Settings,
) -> None:
    checker = _ToggleFfmpegChecker(available=True)
    transport, seen = _capture_transport()
    app = create_app(
        settings=settings,
        disk_usage=_ample_free_space(),
        ffmpeg_checker=checker,
        notify_transport=transport,
    )
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)
        with app.state.session_factory() as session:
            update_notifier_config(
                session, webhook_url="https://example.com/hook", webhook_enabled=True
            )

        # The startup tick already ran with FFmpeg "present" -> a passing row,
        # no notification. Flip the checker so this recheck sees a genuine
        # pass -> fail transition.
        checker.available = False

        response = test_client.post("/api/system/health-checks/recheck", headers=headers)

        assert response.status_code == 200
        row = next(r for r in response.json() if r["code"] == "ffmpeg_missing")
        assert row["status"] == "failing"
        assert row["first_failed_at"] is not None

        # Edge-triggered: exactly one notification for the pass -> fail
        # transition, dispatched through reconcile_health_results' existing
        # transition-detection path (COL-75) -- no separate notification
        # mechanism for a manual recheck.
        assert len(seen) == 1
        payload = json.loads(seen[0].content)
        assert payload["event_type"] == "health_check_failed"
        assert payload["details"]["code"] == "ffmpeg_missing"

        # A second recheck with nothing changed: still failing, but no new
        # notification (edge-, not level-triggered).
        second = test_client.post("/api/system/health-checks/recheck", headers=headers)
        assert second.status_code == 200
        assert len(seen) == 1


def test_recheck_does_not_disrupt_the_background_scheduler(settings: Settings) -> None:
    """A manual recheck is an extra, out-of-band tick, not a scheduler restart:
    it must not touch the periodic background thread or reset its timer."""
    app = create_app(settings=settings, disk_usage=_ample_free_space(), enable_scheduler=True)
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)
        scheduler = app.state.health_scheduler
        thread_before = scheduler._thread

        response = test_client.post("/api/system/health-checks/recheck", headers=headers)

        assert response.status_code == 200
        # Same thread object, still alive and running -- the recheck never
        # touched the loop's `_thread` / `_stop` event.
        assert scheduler._thread is thread_before
        assert scheduler._thread is not None
        assert scheduler._thread.is_alive()
