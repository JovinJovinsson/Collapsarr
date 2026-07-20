"""Contract tests for the notifier config REST endpoints (COL-36).

Covers request/response shape and the API-key-required behaviour (COL-26) for
``GET``/``PUT /api/notifiers``. The endpoints read/write the real persisted
:class:`~collapsarr.notify.models.NotifierConfig` row via
:mod:`collapsarr.notify.service`; assertions round-trip through a fresh
``GET`` to confirm writes actually persist.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr.settings.service import get_global_settings


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


# --- GET shape ----------------------------------------------------------------


def test_get_notifiers_returns_documented_defaults(client: TestClient) -> None:
    response = client.get("/api/notifiers", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["webhook_url"] is None
    assert body["webhook_enabled"] is False
    assert body["discord_webhook_url"] is None
    assert body["discord_enabled"] is False
    assert "created_at" in body
    assert "updated_at" in body


# --- PUT write + round-trip ---------------------------------------------------


def test_put_notifiers_updates_and_persists(client: TestClient) -> None:
    response = client.put(
        "/api/notifiers",
        json={
            "webhook_url": "https://example.com/hook",
            "webhook_enabled": True,
            "discord_webhook_url": "https://discord.com/api/webhooks/123/abc",
            "discord_enabled": True,
        },
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["webhook_url"] == "https://example.com/hook"
    assert body["webhook_enabled"] is True
    assert body["discord_webhook_url"] == "https://discord.com/api/webhooks/123/abc"
    assert body["discord_enabled"] is True

    # A fresh GET reflects the persisted change.
    follow_up = client.get("/api/notifiers", headers=_auth_headers(client))
    assert follow_up.json()["webhook_url"] == "https://example.com/hook"
    assert follow_up.json()["discord_enabled"] is True


def test_put_notifiers_leaves_omitted_fields_untouched(client: TestClient) -> None:
    client.put(
        "/api/notifiers",
        json={"webhook_url": "https://example.com/hook", "webhook_enabled": True},
        headers=_auth_headers(client),
    )

    client.put(
        "/api/notifiers",
        json={"discord_enabled": True},
        headers=_auth_headers(client),
    )

    body = client.get("/api/notifiers", headers=_auth_headers(client)).json()
    # The webhook fields set in the first PUT survive the second, unrelated PUT.
    assert body["webhook_url"] == "https://example.com/hook"
    assert body["webhook_enabled"] is True
    assert body["discord_enabled"] is True
    assert body["discord_webhook_url"] is None


def test_put_notifiers_explicit_null_clears_url(client: TestClient) -> None:
    client.put(
        "/api/notifiers",
        json={"webhook_url": "https://example.com/hook"},
        headers=_auth_headers(client),
    )
    assert (
        client.get("/api/notifiers", headers=_auth_headers(client)).json()["webhook_url"]
        == "https://example.com/hook"
    )

    client.put(
        "/api/notifiers",
        json={"webhook_url": None},
        headers=_auth_headers(client),
    )

    body = client.get("/api/notifiers", headers=_auth_headers(client)).json()
    assert body["webhook_url"] is None


def test_put_notifiers_rejects_unknown_field(client: TestClient) -> None:
    response = client.put(
        "/api/notifiers",
        json={"slack_webhook_url": "https://example.com"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 422


# --- auth-required behaviour ---------------------------------------------------


def test_notifiers_endpoints_require_the_api_key(client: TestClient) -> None:
    for method in ("get", "put"):
        response = client.request(method, "/api/notifiers", json={})
        assert response.status_code == 401, f"{method.upper()} /api/notifiers was not gated"
