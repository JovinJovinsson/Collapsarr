"""Contract tests for the global Settings REST endpoints (COL-28).

Covers request/response shape and the API-key-required behaviour (COL-26) for
``GET``/``PUT /api/settings``. The endpoints read/write the real persisted
:class:`~collapsarr.settings.models.GlobalSettings` row via
:mod:`collapsarr.settings.service`; assertions round-trip through a fresh
``GET`` to confirm writes actually persist.
"""

from __future__ import annotations

import logging
import logging.handlers

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.logging_setup import LOGGER_NAME
from collapsarr.settings.service import get_global_settings, update_global_settings


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
    assert body["auth_required"] == "local_bypass"  # COL-51 default
    assert body["backup_interval_days"] == 7  # COL-66 default
    assert body["backup_retention_days"] == 28  # COL-66 default
    assert body["disk_space_warning_percent"] == 5.0  # COL-79 default
    assert body["disk_space_error_percent"] == 2.0  # COL-79 default
    assert body["update_channel"] == "stable"  # COL-88 default
    assert body["log_level"] is None  # COL-130: unset, falls back to env at boot
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


# --- auth_required mode toggle (COL-51) ----------------------------------------


def test_put_settings_switches_auth_required_mode(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"auth_required": "enabled"},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    assert response.json()["auth_required"] == "enabled"

    # Persisted -- a fresh GET reflects it.
    follow_up = client.get("/api/settings", headers=_auth_headers(client))
    assert follow_up.json()["auth_required"] == "enabled"


def test_put_settings_rejects_an_unknown_auth_required_value(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"auth_required": "disabled"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 422


# --- backup schedule (COL-66) ---------------------------------------------------


def test_put_settings_updates_backup_interval_and_retention(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"backup_interval_days": 3, "backup_retention_days": 14},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["backup_interval_days"] == 3
    assert body["backup_retention_days"] == 14

    # Persisted -- a fresh GET reflects it.
    follow_up = client.get("/api/settings", headers=_auth_headers(client))
    assert follow_up.json()["backup_interval_days"] == 3
    assert follow_up.json()["backup_retention_days"] == 14


def test_put_settings_leaves_backup_schedule_untouched_when_omitted(client: TestClient) -> None:
    client.put(
        "/api/settings",
        json={"backup_interval_days": 5, "backup_retention_days": 20},
        headers=_auth_headers(client),
    )

    client.put(
        "/api/settings",
        json={"concurrency_limit": 3},
        headers=_auth_headers(client),
    )

    body = client.get("/api/settings", headers=_auth_headers(client)).json()
    assert body["backup_interval_days"] == 5
    assert body["backup_retention_days"] == 20
    assert body["concurrency_limit"] == 3


def test_put_settings_rejects_a_zero_backup_interval(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"backup_interval_days": 0},
        headers=_auth_headers(client),
    )
    assert response.status_code == 422


def test_put_settings_rejects_a_negative_backup_retention(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"backup_retention_days": -1},
        headers=_auth_headers(client),
    )
    assert response.status_code == 422


# --- disk-space thresholds (COL-79) --------------------------------------------


def test_put_settings_updates_disk_space_thresholds(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"disk_space_warning_percent": 10.0, "disk_space_error_percent": 3.0},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["disk_space_warning_percent"] == 10.0
    assert body["disk_space_error_percent"] == 3.0

    # Persisted -- a fresh GET reflects it.
    follow_up = client.get("/api/settings", headers=_auth_headers(client))
    assert follow_up.json()["disk_space_warning_percent"] == 10.0
    assert follow_up.json()["disk_space_error_percent"] == 3.0


def test_put_settings_leaves_disk_space_thresholds_untouched_when_omitted(
    client: TestClient,
) -> None:
    client.put(
        "/api/settings",
        json={"disk_space_warning_percent": 8.0, "disk_space_error_percent": 4.0},
        headers=_auth_headers(client),
    )

    client.put(
        "/api/settings",
        json={"concurrency_limit": 3},
        headers=_auth_headers(client),
    )

    body = client.get("/api/settings", headers=_auth_headers(client)).json()
    assert body["disk_space_warning_percent"] == 8.0
    assert body["disk_space_error_percent"] == 4.0
    assert body["concurrency_limit"] == 3


def test_put_settings_rejects_a_zero_disk_space_warning_percent(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"disk_space_warning_percent": 0},
        headers=_auth_headers(client),
    )
    assert response.status_code == 422


def test_put_settings_rejects_a_disk_space_error_percent_above_100(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"disk_space_error_percent": 101},
        headers=_auth_headers(client),
    )
    assert response.status_code == 422


# --- update channel (COL-88) -----------------------------------------------------


def test_put_settings_switches_update_channel(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"update_channel": "beta"},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    assert response.json()["update_channel"] == "beta"

    # Persisted -- a fresh GET reflects it.
    follow_up = client.get("/api/settings", headers=_auth_headers(client))
    assert follow_up.json()["update_channel"] == "beta"


def test_put_settings_update_channel_is_switchable_back_to_stable(client: TestClient) -> None:
    client.put("/api/settings", json={"update_channel": "beta"}, headers=_auth_headers(client))

    response = client.put(
        "/api/settings",
        json={"update_channel": "stable"},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    assert response.json()["update_channel"] == "stable"


def test_put_settings_rejects_an_unknown_update_channel(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"update_channel": "nightly"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 422


def test_put_settings_leaves_update_channel_untouched_when_omitted(client: TestClient) -> None:
    client.put("/api/settings", json={"update_channel": "beta"}, headers=_auth_headers(client))

    client.put("/api/settings", json={"concurrency_limit": 3}, headers=_auth_headers(client))

    body = client.get("/api/settings", headers=_auth_headers(client)).json()
    assert body["update_channel"] == "beta"
    assert body["concurrency_limit"] == 3


# --- log level (COL-130) --------------------------------------------------------


def test_put_settings_switches_log_level(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"log_level": "DEBUG"},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    assert response.json()["log_level"] == "DEBUG"

    # Persisted -- a fresh GET reflects it.
    follow_up = client.get("/api/settings", headers=_auth_headers(client))
    assert follow_up.json()["log_level"] == "DEBUG"


def test_put_settings_rejects_an_unknown_log_level(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"log_level": "TRACE"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 422


def test_put_settings_leaves_log_level_untouched_when_omitted(client: TestClient) -> None:
    client.put("/api/settings", json={"log_level": "DEBUG"}, headers=_auth_headers(client))

    client.put("/api/settings", json={"concurrency_limit": 3}, headers=_auth_headers(client))

    body = client.get("/api/settings", headers=_auth_headers(client)).json()
    assert body["log_level"] == "DEBUG"
    assert body["concurrency_limit"] == 3


def test_put_settings_explicit_null_log_level_clears_the_override(client: TestClient) -> None:
    client.put("/api/settings", json={"log_level": "DEBUG"}, headers=_auth_headers(client))

    response = client.put(
        "/api/settings",
        json={"log_level": None},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    assert response.json()["log_level"] is None


def test_put_settings_log_level_applies_live_without_restart(client: TestClient) -> None:
    """COL-130 AC: the change reaches the running logger/handler immediately."""
    response = client.put(
        "/api/settings",
        json={"log_level": "DEBUG"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 200, response.text

    logger = logging.getLogger(LOGGER_NAME)
    assert logger.level == logging.DEBUG
    file_handler = next(
        h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    assert file_handler.backupCount == 51

    back_to_info = client.put(
        "/api/settings",
        json={"log_level": "WARNING"},
        headers=_auth_headers(client),
    )
    assert back_to_info.status_code == 200, back_to_info.text
    assert logger.level == logging.WARNING
    assert file_handler.backupCount == 6


# --- auth-required behaviour ---------------------------------------------------


def test_settings_endpoints_require_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)

    for method in ("get", "put"):
        response = client.request(method, "/api/settings", json={})
        assert response.status_code == 401, f"{method.upper()} /api/settings was not gated"
