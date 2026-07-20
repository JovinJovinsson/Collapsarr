"""Tests for API-key enforcement on the HTTP API (COL-26).

Exercises the middleware wired by ``create_app``: ``/api`` routes demand a
valid key (``X-Api-Key`` header or ``apikey`` query param), non-API routes stay
open. ``/api/webhook/arr/1`` is used as a representative protected route -- no
instance ``1`` exists in the throwaway DB, so a *pass-through* surfaces as a
``404`` from the handler, cleanly distinguishing "auth passed" from the ``401``
the middleware raises before the handler runs.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.settings.service import get_global_settings

WEBHOOK_ROUTE = "/api/webhook/arr/1"


def test_missing_key_is_rejected(client: TestClient) -> None:
    response = client.post(WEBHOOK_ROUTE, json={})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key."}


def test_invalid_key_is_rejected(client: TestClient) -> None:
    response = client.post(WEBHOOK_ROUTE, json={}, headers={"X-Api-Key": "not-the-key"})

    assert response.status_code == 401


def test_valid_key_in_header_passes_through(client: TestClient, session: Session) -> None:
    key = get_global_settings(session).api_key

    response = client.post(WEBHOOK_ROUTE, json={}, headers={"X-Api-Key": key})

    # Auth passed; the handler then 404s because instance 1 is not configured.
    assert response.status_code == 404


def test_valid_key_in_query_param_passes_through(client: TestClient, session: Session) -> None:
    key = get_global_settings(session).api_key

    response = client.post(f"{WEBHOOK_ROUTE}?apikey={key}", json={})

    assert response.status_code == 404


def test_health_route_is_exempt(client: TestClient) -> None:
    """The liveness probe is unauthenticated (Sonarr/Radarr /ping convention)."""
    assert client.get("/health").status_code == 200
