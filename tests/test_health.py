"""Smoke tests for the application skeleton."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr import __version__


def test_health_returns_ok(client: TestClient) -> None:
    """GET /health returns 200 with a JSON status payload."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_app_wires_database_state(client: TestClient) -> None:
    """The lifespan sets up the engine and session factory on app.state."""
    app = client.app
    assert isinstance(app, FastAPI)
    assert app.state.engine is not None
    assert app.state.session_factory is not None
