"""Contract tests for the Self-Update status endpoint (COL-230).

Covers ``GET /api/system/self-update/status``: the API-gate behaviour (401
without a key/session, mirroring ``tests/test_update_check_routes.py``), the
response shape, and that it get-or-creates the singleton row rather than
404ing before any self-update flow has ever run.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.self_update.models import PHASE_VERIFYING
from collapsarr.self_update.service import begin_self_update, set_self_update_phase
from collapsarr.settings.service import get_global_settings


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #


def test_get_status_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/system/self-update/status").status_code == 401


# --------------------------------------------------------------------------- #
# Response shape
# --------------------------------------------------------------------------- #


def test_get_status_returns_idle_before_any_self_update_has_run(client: TestClient) -> None:
    response = client.get("/api/system/self-update/status", headers=_auth_headers(client))

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "in_progress": False,
        "phase": "idle",
        "previous_version": None,
    }


def test_get_status_reflects_an_in_progress_self_update(client: TestClient) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        assert isinstance(session, Session)
        begin_self_update(session, previous_version="1.0.0")
        set_self_update_phase(session, PHASE_VERIFYING)

    response = client.get("/api/system/self-update/status", headers=_auth_headers(client))

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "in_progress": True,
        "phase": "verifying",
        "previous_version": "1.0.0",
    }
