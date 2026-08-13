"""Contract tests for the Plex connection REST endpoints (COL-209).

Covers request/response shape, the API-key-required behaviour (COL-26), and
-- most importantly for this ticket -- that the token is never present
anywhere in a response body. Connectivity is not stubbed here (mirroring
``test_arr_routes.py``): the update handler calls the real service, which
runs a connectivity check against ``base_url``; tests point the connection at
an unreachable local port so the check fails fast and the row persists with
``status="error"``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.settings.service import get_global_settings, update_global_settings

# Nothing listens here, so the service's connectivity check fails immediately
# rather than blocking on a DNS/connect timeout.
UNREACHABLE_URL = "http://127.0.0.1:9"


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _put_connection(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.put("/api/plex/connection", json=body, headers=_auth_headers(client))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


# --- GET shape -----------------------------------------------------------------


def test_get_plex_connection_returns_documented_defaults(client: TestClient) -> None:
    response = client.get("/api/plex/connection", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["base_url"] == ""
    assert body["has_token"] is False
    assert body["status"] == "unknown"
    assert body["status_error"] is None
    assert body["status_checked_at"] is None
    assert body["version"] is None
    assert "created_at" in body
    assert "updated_at" in body
    assert "token" not in body


# --- PUT write + round-trip ------------------------------------------------------


def test_put_plex_connection_updates_and_persists(client: TestClient) -> None:
    body = _put_connection(client, base_url=UNREACHABLE_URL, token="plex-secret-token")

    assert body["base_url"] == UNREACHABLE_URL
    assert body["has_token"] is True
    # Connectivity was attempted and recorded (unreachable -> error).
    assert body["status"] == "error"
    assert body["status_error"] is not None

    follow_up = client.get("/api/plex/connection", headers=_auth_headers(client))
    assert follow_up.json()["base_url"] == UNREACHABLE_URL
    assert follow_up.json()["has_token"] is True


def test_put_plex_connection_leaves_omitted_fields_untouched(client: TestClient) -> None:
    _put_connection(client, base_url=UNREACHABLE_URL, token="plex-secret-token")

    updated = _put_connection(client, base_url="http://127.0.0.1:10")

    assert updated["base_url"] == "http://127.0.0.1:10"
    # Token was not sent on the second PUT -- still configured.
    assert updated["has_token"] is True


def test_put_plex_connection_rejects_unknown_field(client: TestClient) -> None:
    response = client.put(
        "/api/plex/connection",
        json={"api_key": "nope"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 422


# --- token never reaches the response -------------------------------------------


def test_token_never_appears_in_get_response(client: TestClient) -> None:
    _put_connection(client, base_url=UNREACHABLE_URL, token="super-secret-plex-token")

    response = client.get("/api/plex/connection", headers=_auth_headers(client))

    assert "super-secret-plex-token" not in response.text
    assert "token" not in response.json()


def test_token_never_appears_in_put_response(client: TestClient) -> None:
    response = client.put(
        "/api/plex/connection",
        json={"base_url": UNREACHABLE_URL, "token": "super-secret-plex-token"},
        headers=_auth_headers(client),
    )

    assert "super-secret-plex-token" not in response.text
    assert "token" not in response.json()


def test_token_never_appears_in_openapi_schema_response_shape(client: TestClient) -> None:
    """The response_model itself has no token field, so no OpenAPI branch can leak it."""
    response = client.get("/openapi.json", headers=_auth_headers(client))
    assert response.status_code == 200
    schema = response.json()
    plex_read_schema = schema["components"]["schemas"].get("PlexConnectionRead")
    assert plex_read_schema is not None
    assert "token" not in plex_read_schema.get("properties", {})


# --- Plex Sync manual trigger + on-save hook (COL-210) --------------------------


class _SpyScheduler:
    """Stands in for the wired ``PlexSyncScheduler`` to observe the route's calls."""

    def __init__(self) -> None:
        self.request_sync_calls = 0
        self.run_once_calls = 0

    def request_sync(self) -> None:
        self.request_sync_calls += 1

    def run_once(self) -> int:
        self.run_once_calls += 1
        return 7


def _install_spy_scheduler(client: TestClient) -> _SpyScheduler:
    """Swap the wired Plex Sync scheduler for a spy, to observe the route's calls."""
    app = client.app
    assert isinstance(app, FastAPI)
    spy = _SpyScheduler()
    app.state.plex_sync_scheduler = spy
    return spy


def test_saving_the_connection_triggers_a_background_sync(client: TestClient) -> None:
    spy = _install_spy_scheduler(client)

    _put_connection(client, base_url=UNREACHABLE_URL, token="plex-secret-token")

    # The save asked for an off-cycle sync (non-blocking request_sync), and did
    # not itself run one synchronously on the request thread.
    assert spy.request_sync_calls == 1
    assert spy.run_once_calls == 0


def test_run_now_endpoint_runs_a_sync_and_reports_the_row_count(client: TestClient) -> None:
    spy = _install_spy_scheduler(client)

    response = client.post("/api/plex/sync", headers=_auth_headers(client))

    assert response.status_code == 202, response.text
    assert response.json() == {"items": 7}
    assert spy.run_once_calls == 1


def test_run_now_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)

    response = client.post("/api/plex/sync", json={})

    assert response.status_code == 401


# --- auth-required behaviour ---------------------------------------------------


def test_plex_connection_endpoints_require_the_api_key(
    client: TestClient, session: Session
) -> None:
    update_global_settings(session, ui_auth_enabled=True)

    for method in ("get", "put"):
        response = client.request(method, "/api/plex/connection", json={})
        assert response.status_code == 401, f"{method.upper()} /api/plex/connection was not gated"
