"""Contract tests for the global Settings REST endpoints (COL-28).

Covers request/response shape and the API-key-required behaviour (COL-26) for
``GET``/``PUT /api/settings``. The endpoints read/write the real persisted
:class:`~collapsarr.settings.models.GlobalSettings` row via
:mod:`collapsarr.settings.service`; assertions round-trip through a fresh
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


def test_get_settings_returns_documented_defaults(client: TestClient) -> None:
    response = client.get("/api/settings", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled_targets"] == ["stereo"]
    assert body["language_allow_list"] is None
    assert body["stereo_codec"] == "aac"
    assert body["stereo_bitrate_kbps"] is None
    assert body["surround_codec"] == "ac3"
    assert body["surround_bitrate_kbps"] == 448
    assert body["concurrency_limit"] == 1
    assert body["ui_auth_enabled"] is False
    assert body["api_key"]  # auto-generated, surfaced read-only
    assert "created_at" in body
    assert "updated_at" in body


# --- PUT write + round-trip ---------------------------------------------------


def test_put_settings_updates_and_persists(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={
            "enabled_targets": ["stereo", "5.1"],
            "language_allow_list": ["eng", "jpn"],
            "surround_bitrate_kbps": 640,
            "concurrency_limit": 4,
            "ui_auth_enabled": True,
        },
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled_targets"] == ["5.1", "stereo"]  # sorted by value
    assert body["language_allow_list"] == ["eng", "jpn"]  # sorted
    assert body["surround_bitrate_kbps"] == 640
    assert body["concurrency_limit"] == 4
    assert body["ui_auth_enabled"] is True

    # A fresh GET reflects the persisted change.
    follow_up = client.get("/api/settings", headers=_auth_headers(client))
    assert follow_up.json()["concurrency_limit"] == 4
    assert follow_up.json()["enabled_targets"] == ["5.1", "stereo"]


def test_put_settings_leaves_omitted_fields_untouched(client: TestClient) -> None:
    client.put(
        "/api/settings",
        json={"concurrency_limit": 7},
        headers=_auth_headers(client),
    )

    body = client.get("/api/settings", headers=_auth_headers(client)).json()
    assert body["concurrency_limit"] == 7
    # Untouched defaults survive.
    assert body["stereo_codec"] == "aac"
    assert body["surround_bitrate_kbps"] == 448
    assert body["enabled_targets"] == ["stereo"]


def test_put_settings_explicit_null_clears_override(client: TestClient) -> None:
    # Seed a language allow-list, then clear it with an explicit null.
    client.put(
        "/api/settings",
        json={"language_allow_list": ["eng"]},
        headers=_auth_headers(client),
    )
    assert client.get("/api/settings", headers=_auth_headers(client)).json()[
        "language_allow_list"
    ] == ["eng"]

    client.put(
        "/api/settings",
        json={"language_allow_list": None, "surround_bitrate_kbps": None},
        headers=_auth_headers(client),
    )

    body = client.get("/api/settings", headers=_auth_headers(client)).json()
    assert body["language_allow_list"] is None
    assert body["surround_bitrate_kbps"] is None


def test_put_settings_rejects_unknown_target(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"enabled_targets": ["quadraphonic"]},
        headers=_auth_headers(client),
    )
    assert response.status_code == 422


# --- auth-required behaviour ---------------------------------------------------


def test_settings_endpoints_require_the_api_key(client: TestClient) -> None:
    for method in ("get", "put"):
        response = client.request(method, "/api/settings", json={})
        assert response.status_code == 401, f"{method.upper()} /api/settings was not gated"
