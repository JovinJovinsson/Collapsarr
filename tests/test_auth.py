"""Tests for the unconditional auth gate wired by ``create_app`` (COL-50).

Exercises the enforcement + session middleware and the ``/api/auth`` routes:
the app launches with zero config, but every UI route redirects to ``/setup``
until a credential exists, and to ``/login`` until a session exists; ``/api``
passes with a valid session **or** the API key; ``/health`` is always open.

Two pass-through tricks distinguish "auth passed" from "auth rejected", since
there is no built SPA in a source checkout:

* ``/api/webhook/arr/1`` -- no instance ``1`` exists, so a pass-through surfaces
  as a ``404`` from the handler, cleanly separated from the ``401`` the
  middleware raises before the handler runs.
* a UI route like ``/wanted`` -- with no SPA mounted, a pass-through surfaces as
  a ``404`` (no route), separated from the ``303`` redirect the gate issues.

The old ``ui_auth_enabled`` opt-in no longer governs access (COL-50 supersedes
COL-26/COL-45): enforcement is unconditional once a credential is set.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.main import create_app
from collapsarr.settings.service import get_global_settings, update_global_settings

WEBHOOK_ROUTE = "/api/webhook/arr/1"
UI_ROUTE = "/wanted"

USERNAME = "operator"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def noredirect_client(settings: Settings) -> Iterator[TestClient]:
    """A client that surfaces redirects rather than following them, so the
    gate's ``303``/``Location`` is observable."""
    app = create_app(settings=settings)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def _set_credential(session: Session) -> str:
    """Persist a credential directly (bypassing the setup route) and return the
    server API key. Shares the DB file with the ``client`` app fixture."""
    update_global_settings(session, auth_username=USERNAME, password=PASSWORD)
    return get_global_settings(session).api_key


# --- fresh install: first-run gate -------------------------------------------


def test_health_is_open_on_a_fresh_install(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_ui_route_redirects_to_setup_when_no_credential(noredirect_client: TestClient) -> None:
    response = noredirect_client.get(UI_ROUTE)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_setup_page_is_served_when_no_credential(noredirect_client: TestClient) -> None:
    # No SPA mounted, so "served" surfaces as a 404 (not a redirect).
    assert noredirect_client.get("/setup").status_code == 404


def test_api_requires_a_key_even_on_a_fresh_install(client: TestClient, session: Session) -> None:
    """Enforcement is unconditional -- no opt-in. Without a key: 401."""
    assert client.post(WEBHOOK_ROUTE, json={}).status_code == 401


def test_api_with_a_valid_key_passes_on_a_fresh_install(
    client: TestClient, session: Session
) -> None:
    key = get_global_settings(session).api_key

    response = client.post(WEBHOOK_ROUTE, json={}, headers={"X-Api-Key": key})

    assert response.status_code == 404  # auth passed; handler 404s on instance 1


# --- setup closes the gate ---------------------------------------------------


def test_setup_persists_a_hashed_credential_and_logs_in(
    client: TestClient, session: Session
) -> None:
    response = client.post(
        "/api/auth/setup", json={"username": USERNAME, "password": PASSWORD}
    )

    assert response.status_code == 200
    assert response.json() == {"needs_setup": False, "authenticated": True}

    settings = get_global_settings(session)
    assert settings.auth_username == USERNAME
    # Stored as a PBKDF2 hash, never plaintext.
    assert settings.auth_password_hash is not None
    assert PASSWORD not in settings.auth_password_hash

    # The gate is now closed: the setup response set a session cookie, so a UI
    # route passes through (404, no SPA) rather than redirecting.
    assert client.get(UI_ROUTE).status_code == 404


def test_setup_is_rejected_once_a_credential_exists(
    client: TestClient, session: Session
) -> None:
    _set_credential(session)

    response = client.post(
        "/api/auth/setup", json={"username": "other", "password": "another"}
    )

    assert response.status_code == 409


# --- login / logout ----------------------------------------------------------


def test_login_before_setup_is_conflict(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})

    assert response.status_code == 409


def test_login_with_wrong_password_is_rejected(client: TestClient, session: Session) -> None:
    _set_credential(session)

    response = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": "wrong"}
    )

    assert response.status_code == 401


def test_login_with_correct_credential_grants_ui_access(
    client: TestClient, session: Session
) -> None:
    _set_credential(session)

    login = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    assert login.status_code == 200
    assert "collapsarr_session" in login.cookies

    # The client now carries the session cookie: a UI route passes through.
    assert client.get(UI_ROUTE).status_code == 404


def test_remember_me_produces_a_longer_lived_cookie(
    noredirect_client: TestClient, session: Session
) -> None:
    _set_credential(session)

    persistent = noredirect_client.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD, "remember": True},
    )
    session_only = noredirect_client.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD, "remember": False},
    )

    assert "Max-Age" in persistent.headers["set-cookie"]
    assert "Max-Age" not in session_only.headers["set-cookie"]


def test_logout_clears_the_session_and_returns_to_login(
    noredirect_client: TestClient, session: Session
) -> None:
    _set_credential(session)

    noredirect_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    # Logged in: UI route passes through.
    assert noredirect_client.get(UI_ROUTE).status_code == 404

    logout = noredirect_client.post("/api/auth/logout")
    assert logout.status_code == 200
    # Cookie is discarded (expired in the past).
    assert "expires=Thu, 01 Jan 1970" in logout.headers["set-cookie"]

    # Session gone, credential still set: UI routes redirect to /login now.
    redirect = noredirect_client.get(UI_ROUTE)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/login"


# --- /api: session OR key ----------------------------------------------------


def test_api_is_reachable_with_a_valid_session(client: TestClient, session: Session) -> None:
    _set_credential(session)
    client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})

    # No API key attached, but the session cookie authenticates the request.
    assert client.get("/api/settings").status_code == 200


def test_webhook_still_works_with_query_param_key(client: TestClient, session: Session) -> None:
    key = _set_credential(session)

    # A Sonarr/Radarr webhook can only set a query string, not a header.
    response = client.post(f"{WEBHOOK_ROUTE}?apikey={key}", json={})

    assert response.status_code == 404  # auth passed; handler 404s on instance 1


def test_api_without_session_or_key_is_rejected(client: TestClient, session: Session) -> None:
    _set_credential(session)

    assert client.get("/api/settings").status_code == 401


def test_health_stays_open_once_a_credential_exists(
    client: TestClient, session: Session
) -> None:
    _set_credential(session)

    assert client.get("/health").status_code == 200


def test_ui_auth_enabled_toggle_no_longer_governs_access(
    client: TestClient, session: Session
) -> None:
    """Enforcement is unconditional: the legacy flag doesn't change the gate."""
    key = _set_credential(session)
    # Explicitly leave the legacy opt-in off.
    update_global_settings(session, ui_auth_enabled=False)

    # Still rejected without auth...
    assert client.post(WEBHOOK_ROUTE, json={}).status_code == 401
    # ...and still reachable with the key.
    assert client.post(WEBHOOK_ROUTE, json={}, headers={"X-Api-Key": key}).status_code == 404
