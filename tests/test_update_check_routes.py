"""Contract tests for the Update Check REST endpoints (COL-87).

Covers ``GET /api/system/updates``: the API-gate behaviour (401 without a
key/session), the response shape (running version, latest version, changelog,
checked_at, update_available), and both the "up to date" and "update
available" states.

Also covers ``POST /api/system/updates/recheck``: the auth gate, that it runs
a real tick and returns the refreshed state, that a manual recheck is
reflected by a subsequent ``GET``, and that it never disturbs the background
scheduler's own periodic thread/timer -- the same guarantees
``tests/test_health_routes.py`` asserts for the health-checks recheck
endpoint.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr import __version__
from collapsarr.config import Settings
from collapsarr.main import create_app
from collapsarr.settings.service import get_global_settings
from collapsarr.update_check.comparison import running_version_tag


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _release_transport(tag: str, *, name: str = "", body: str = "") -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tag_name": tag,
                "name": name,
                "body": body,
                "published_at": "2026-08-01T00:00:00Z",
            },
        )

    return httpx.MockTransport(handler)


def _app_with_release(
    settings: Settings, tag: str, *, name: str = "", body: str = ""
) -> FastAPI:
    return create_app(
        settings=settings,
        update_check_transport=_release_transport(tag, name=name, body=body),
    )


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #


def test_get_updates_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/system/updates").status_code == 401


def test_recheck_requires_authentication(client: TestClient) -> None:
    assert client.post("/api/system/updates/recheck").status_code == 401


# --------------------------------------------------------------------------- #
# GET /api/system/updates -- shape + states
# --------------------------------------------------------------------------- #


def test_get_updates_before_any_successful_fetch(client: TestClient) -> None:
    """The shared `client` fixture's startup tick uses an offline transport
    (see `conftest.py`'s `_offline_update_check_transport`), so `checked_at`
    is set (a tick ran) but no release data was ever fetched successfully."""
    headers = _auth_headers(client)

    response = client.get("/api/system/updates", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["running_version"] == __version__
    assert body["latest_version"] is None
    assert body["latest_version_label"] is None
    assert body["changelog"] is None
    assert body["checked_at"] is not None
    # No confirmed match -- "unknown" reports as an update possibly being
    # available rather than falsely claiming the instance is current.
    assert body["update_available"] is True


def test_get_updates_reports_up_to_date_when_latest_tag_matches_running_version(
    settings: Settings,
) -> None:
    app = _app_with_release(settings, running_version_tag(__version__), name="Current release")
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.get("/api/system/updates", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["latest_version"] == running_version_tag(__version__)
        assert body["latest_version_label"] == "Current release"
        assert body["update_available"] is False


def test_get_updates_reports_update_available_when_latest_tag_differs(
    settings: Settings,
) -> None:
    app = _app_with_release(
        settings, "v999.0.0", name="Big new release", body="- shiny new stuff"
    )
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.get("/api/system/updates", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["running_version"] == __version__
        assert body["latest_version"] == "v999.0.0"
        assert body["latest_version_label"] == "Big new release"
        assert body["changelog"] == "- shiny new stuff"
        assert body["update_available"] is True


# --------------------------------------------------------------------------- #
# POST /api/system/updates/recheck
# --------------------------------------------------------------------------- #


def test_recheck_runs_a_tick_and_returns_the_refreshed_state(settings: Settings) -> None:
    # Startup tick sees no release yet (default offline-ish transport isn't
    # used here -- build the app with an explicit release transport so the
    # *recheck* is what actually surfaces it, distinguishing "state before"
    # from "state after").
    app = _app_with_release(settings, "v2.0.0", name="v2.0.0", body="notes")
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.post("/api/system/updates/recheck", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["latest_version"] == "v2.0.0"
        assert body["update_available"] is True

        # And it's live, not cached: a subsequent GET reflects the same,
        # freshly persisted state.
        get_body = test_client.get("/api/system/updates", headers=headers).json()
        assert get_body["latest_version"] == "v2.0.0"
        assert get_body["checked_at"] == body["checked_at"]


def test_recheck_does_not_disrupt_the_background_scheduler(settings: Settings) -> None:
    """A manual recheck is an extra, out-of-band tick, not a scheduler restart:
    it must not touch the periodic background thread or reset its timer."""
    app = create_app(
        settings=settings,
        enable_scheduler=True,
        update_check_transport=_release_transport("v1.0.0"),
    )
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)
        scheduler = app.state.update_check_scheduler
        thread_before = scheduler._thread

        response = test_client.post("/api/system/updates/recheck", headers=headers)

        assert response.status_code == 200
        # Same thread object, still alive and running -- the recheck never
        # touched the loop's `_thread` / `_stop` event.
        assert scheduler._thread is thread_before
        assert scheduler._thread is not None
        assert scheduler._thread.is_alive()
