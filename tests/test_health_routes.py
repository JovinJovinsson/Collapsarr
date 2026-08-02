"""Contract tests for the health-check detail REST endpoint (COL-76).

Covers ``GET /api/system/health-checks``: the API-gate behaviour (401 without
a key/session), the full per-row shape (code, category, severity, status,
message, instance_id, first/last-checked timing), and that it works
generically for any number/kind of registered check with mixed
severities/statuses -- not hardcoded to FFmpeg.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.health import SEVERITY_ERROR, SEVERITY_WARNING, HealthCheckResult
from collapsarr.health.service import reconcile_health_results
from collapsarr.settings.service import get_global_settings


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
    # 7, not 3: the shared `client` fixture's app already ran its own startup
    # tick (test_health_check.py, test_health_arr_instances.py,
    # test_health_disk_space.py), persisting a passing FFmpeg row, a failing
    # no-Arr-instances (COL-77) row, and a passing disk-space warning/error
    # pair (COL-79, the fixture pins disk usage to 90% free) -- 4 rows, plus
    # the 3 freshly-seeded ones above -- proving the endpoint lists *every*
    # check, seeded ones alongside it.
    assert len(body) == 7
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
    # are configured (a fresh settings/database fixture), and the fixture
    # pins disk usage to 90% free (COL-79) -- so exactly four rows are
    # persisted before any test seeds anything else: a passing FFmpeg row, a
    # failing no-Arr-instances (COL-77) row, and a passing disk-space
    # warning/error pair (COL-79).
    headers = _auth_headers(client)
    response = client.get("/api/system/health-checks", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 4
    by_code = {row["code"]: row for row in body}
    assert by_code["ffmpeg_missing"]["status"] == "passing"
    assert by_code["WARN-ARR-001"]["status"] == "failing"
    assert by_code["WARN-DISK-001"]["status"] == "passing"
    assert by_code["ERR-DISK-001"]["status"] == "passing"
