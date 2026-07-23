"""Tests for API-key enforcement on the HTTP API (COL-26).

Exercises the middleware wired by ``create_app``: ``/api`` routes demand a
valid key (``X-Api-Key`` header or ``apikey`` query param) once
``ui_auth_enabled`` is turned on, non-API routes always stay open.
``/api/webhook/arr/1`` is used as a representative protected route -- no
instance ``1`` exists in the throwaway DB, so a *pass-through* surfaces as a
``404`` from the handler, cleanly distinguishing "auth passed" from the ``401``
the middleware raises before the handler runs.

``ui_auth_enabled`` defaults to ``False`` (COL-45): a fresh install has no way
to learn its auto-generated key without an already-authenticated request, so
enforcement must be opt-in or the very first ``GET /api/settings`` -- where
the key is displayed -- would 401 before anyone could ever retrieve it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.settings.service import get_global_settings, update_global_settings

WEBHOOK_ROUTE = "/api/webhook/arr/1"


def test_missing_key_passes_through_by_default(client: TestClient) -> None:
    """``ui_auth_enabled`` defaults to ``False``, so no key is required yet."""
    response = client.post(WEBHOOK_ROUTE, json={})

    assert response.status_code == 404


def test_settings_readable_without_a_key_on_a_fresh_install(client: TestClient) -> None:
    """The key itself must be retrievable before enforcement can ever be turned on."""
    response = client.get("/api/settings")

    assert response.status_code == 200
    assert response.json()["api_key"]


def test_missing_key_is_rejected_once_auth_enabled(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)

    response = client.post(WEBHOOK_ROUTE, json={})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key."}


def test_invalid_key_is_rejected_once_auth_enabled(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)

    response = client.post(WEBHOOK_ROUTE, json={}, headers={"X-Api-Key": "not-the-key"})

    assert response.status_code == 401


def test_valid_key_in_header_passes_through_once_auth_enabled(
    client: TestClient, session: Session
) -> None:
    key = get_global_settings(session).api_key
    update_global_settings(session, ui_auth_enabled=True)

    response = client.post(WEBHOOK_ROUTE, json={}, headers={"X-Api-Key": key})

    # Auth passed; the handler then 404s because instance 1 is not configured.
    assert response.status_code == 404


def test_valid_key_in_query_param_passes_through_once_auth_enabled(
    client: TestClient, session: Session
) -> None:
    key = get_global_settings(session).api_key
    update_global_settings(session, ui_auth_enabled=True)

    response = client.post(f"{WEBHOOK_ROUTE}?apikey={key}", json={})

    assert response.status_code == 404


def test_health_route_is_exempt(client: TestClient) -> None:
    """The liveness probe is unauthenticated (Sonarr/Radarr /ping convention)."""
    assert client.get("/health").status_code == 200
