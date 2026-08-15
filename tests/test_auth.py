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

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.main import create_app
from collapsarr.settings.models import AUTH_METHOD_BASIC, AUTH_REQUIRED_ENABLED
from collapsarr.settings.service import get_global_settings, update_global_settings

WEBHOOK_ROUTE = "/api/webhook/arr/1"
UI_ROUTE = "/wanted"

USERNAME = "operator"
PASSWORD = "correct horse battery staple"

LOOPBACK_HOST = "127.0.0.1"
PRIVATE_HOST = "192.168.1.50"
PUBLIC_HOST = "8.8.8.8"  # a real, globally-routable address (Google Public DNS)
TRUSTED_PROXY_HOST = "10.0.0.1"

URL_BASE = "/collapsarr"


@pytest.fixture
def noredirect_client(settings: Settings) -> Iterator[TestClient]:
    """A client that surfaces redirects rather than following them, so the
    gate's ``303``/``Location`` is observable."""
    app = create_app(settings=settings)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def _basic_header(username: str, password: str) -> dict[str, str]:
    """Build an ``Authorization: Basic`` header value for test requests."""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


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
    assert response.json() == {
        "needs_setup": False,
        "authenticated": True,
        "auth_method": "forms",
    }

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


def test_api_path_with_a_file_extension_still_requires_auth(
    client: TestClient, session: Session
) -> None:
    """The static-asset bypass must not open an ``/api`` route (COL-65).

    A backup id ends in ``.zip``, so ``DELETE /api/system/backup/{type}/{file}.zip``
    has a dotted final segment. Without the ``/api`` guard on the static-asset
    check, the extension heuristic would misclassify it as a public bundle asset
    and skip the session/key gate -- silently un-authing the delete endpoint.
    """
    _set_credential(session)

    assert (
        client.delete("/api/system/backup/manual/whatever.zip").status_code == 401
    )


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


# --- local_bypass required-mode (COL-51) --------------------------------------
#
# ``client``/``noredirect_client`` above use the stock TestClient, whose
# default ASGI-scope peer ("testclient", 50000) is not a literal IP -- see
# ``_client_is_local`` in ``collapsarr/auth/enforcement.py`` -- so it always
# classifies as non-local and every test above is unaffected by local_bypass
# becoming the default. These tests build their own TestClient with an
# explicit ``client=(host, port)`` to control what peer address the
# middleware sees.


@contextmanager
def _client_for_peer(settings: Settings, host: str) -> Iterator[TestClient]:
    """A no-follow-redirects TestClient whose ASGI scope reports ``host`` as
    the direct connection peer, so ``_client_is_local`` classifies on it."""
    app = create_app(settings=settings)
    with TestClient(app, client=(host, 51234), follow_redirects=False) as test_client:
        yield test_client


def _settings_with_trusted_proxies(tmp_path: Path, trusted_proxies: str) -> Settings:
    """A ``Settings`` instance like the ``settings`` fixture's, but with
    ``COLLAPSARR_TRUSTED_PROXIES`` set (COL-113) -- for tests that need a
    peer on the trusted-proxy allowlist. Takes ``tmp_path`` directly (rather
    than the ``settings`` fixture) since that fixture bakes in an empty
    allowlist."""
    db_path = tmp_path / "collapsarr.db"
    return Settings(
        database_path=str(db_path), data_dir=str(tmp_path), trusted_proxies=trusted_proxies
    )


def _seed_credential(test_client: TestClient, **auth_kwargs: object) -> None:
    """Persist a credential (and any extra ``update_global_settings`` kwargs,
    e.g. ``auth_required=...``) on ``test_client``'s app, mirroring
    ``_set_credential`` above but for a client built outside the ``client``
    fixture."""
    app = test_client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        update_global_settings(session, auth_username=USERNAME, password=PASSWORD, **auth_kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("host", [LOOPBACK_HOST, PRIVATE_HOST])
def test_local_bypass_default_lets_a_local_client_reach_the_ui_without_logging_in(
    settings: Settings, host: str
) -> None:
    with _client_for_peer(settings, host) as test_client:
        # auth_required is left at its local_bypass default.
        _seed_credential(test_client)

        # No setup, no login -- the UI route passes straight through.
        assert test_client.get(UI_ROUTE).status_code == 404
        # /api is frictionless too: no session, no API key attached.
        assert test_client.post(WEBHOOK_ROUTE, json={}).status_code == 404


@pytest.mark.parametrize("host", [LOOPBACK_HOST, PRIVATE_HOST])
def test_local_bypass_default_skips_first_run_setup_for_a_local_client(
    settings: Settings, host: str
) -> None:
    """Frictionless extends to a totally fresh install: a local caller never
    has to touch /setup to reach the app."""
    with _client_for_peer(settings, host) as test_client:
        assert test_client.get(UI_ROUTE).status_code == 404


def test_local_bypass_still_challenges_a_non_local_client(settings: Settings) -> None:
    with _client_for_peer(settings, PUBLIC_HOST) as test_client:
        _seed_credential(test_client)

        redirect = test_client.get(UI_ROUTE)
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/login"
        assert test_client.post(WEBHOOK_ROUTE, json={}).status_code == 401


@pytest.mark.parametrize("host", [LOOPBACK_HOST, PRIVATE_HOST, PUBLIC_HOST])
def test_enabled_mode_challenges_every_client_regardless_of_address(
    settings: Settings, host: str
) -> None:
    with _client_for_peer(settings, host) as test_client:
        _seed_credential(test_client, auth_required=AUTH_REQUIRED_ENABLED)

        redirect = test_client.get(UI_ROUTE)
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/login"
        assert test_client.post(WEBHOOK_ROUTE, json={}).status_code == 401


def test_local_bypass_classification_ignores_x_forwarded_for_spoofing(
    settings: Settings,
) -> None:
    """Classification uses the direct peer only -- a forged header claiming a
    loopback origin from a real external peer must not grant the bypass."""
    with _client_for_peer(settings, PUBLIC_HOST) as test_client:
        _seed_credential(test_client)

        redirect = test_client.get(UI_ROUTE, headers={"X-Forwarded-For": LOOPBACK_HOST})
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/login"


# --- local_bypass + trusted-proxy resolution (COL-113) ------------------------
#
# ``_client_is_local`` now classifies on ``resolve_client_address``
# (collapsarr.auth.trust, COL-112) rather than the raw ASGI peer directly.
# With no ``COLLAPSARR_TRUSTED_PROXIES`` configured -- every test above,
# including the "ignores spoofing" one -- that resolves to exactly the
# direct peer, so this module's existing suite passing unmodified already
# demonstrates the "byte-for-byte identical by default" AC. These tests
# configure an allowlist to exercise the new trusted-proxy-aware path.


def test_local_bypass_classifies_on_forwarded_for_when_peer_is_a_trusted_proxy(
    tmp_path: Path,
) -> None:
    """The proxy's own peer address is irrelevant once it's trusted -- a
    public rightmost X-Forwarded-For entry is classified non-local and
    challenged, even though the proxy itself connected from a private
    address."""
    settings = _settings_with_trusted_proxies(tmp_path, f"{TRUSTED_PROXY_HOST}/32")
    with _client_for_peer(settings, TRUSTED_PROXY_HOST) as test_client:
        _seed_credential(test_client)

        redirect = test_client.get(UI_ROUTE, headers={"X-Forwarded-For": PUBLIC_HOST})
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/login"
        assert (
            test_client.post(
                WEBHOOK_ROUTE, json={}, headers={"X-Forwarded-For": PUBLIC_HOST}
            ).status_code
            == 401
        )


@pytest.mark.parametrize("forwarded_host", [LOOPBACK_HOST, PRIVATE_HOST])
def test_local_bypass_treats_forwarded_loopback_or_private_as_local_when_peer_is_trusted(
    tmp_path: Path, forwarded_host: str
) -> None:
    """A loopback/private rightmost X-Forwarded-For entry from a trusted
    proxy is classified local and skips auth, same as a direct local peer
    would."""
    settings = _settings_with_trusted_proxies(tmp_path, f"{TRUSTED_PROXY_HOST}/32")
    with _client_for_peer(settings, TRUSTED_PROXY_HOST) as test_client:
        _seed_credential(test_client)

        assert (
            test_client.get(UI_ROUTE, headers={"X-Forwarded-For": forwarded_host}).status_code
            == 404
        )
        assert (
            test_client.post(
                WEBHOOK_ROUTE, json={}, headers={"X-Forwarded-For": forwarded_host}
            ).status_code
            == 404
        )


def test_local_bypass_ignores_forwarded_for_from_a_peer_not_on_the_allowlist(
    tmp_path: Path,
) -> None:
    """A peer that is not a trusted proxy is classified by its own direct
    address regardless -- a forwarded header claiming loopback from a
    genuinely public, untrusted peer has no effect."""
    settings = _settings_with_trusted_proxies(tmp_path, f"{TRUSTED_PROXY_HOST}/32")
    with _client_for_peer(settings, PUBLIC_HOST) as test_client:
        _seed_credential(test_client)

        redirect = test_client.get(UI_ROUTE, headers={"X-Forwarded-For": LOOPBACK_HOST})
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/login"


def test_local_bypass_ignores_forwarded_for_from_an_untrusted_local_peer(
    tmp_path: Path,
) -> None:
    """The inverse spoof attempt: an untrusted peer that IS local isn't
    knocked out of the bypass by a forwarded header claiming a public
    address either -- the header is only ever consulted for an allow-listed
    peer."""
    settings = _settings_with_trusted_proxies(tmp_path, f"{TRUSTED_PROXY_HOST}/32")
    with _client_for_peer(settings, LOOPBACK_HOST) as test_client:
        _seed_credential(test_client)

        assert (
            test_client.get(UI_ROUTE, headers={"X-Forwarded-For": PUBLIC_HOST}).status_code == 404
        )


# --- session cookie Secure flag + trusted-proxy resolution (COL-114) ---------
#
# ``_is_secure`` (collapsarr.auth.session) now resolves the scheme through
# ``resolve_scheme`` (collapsarr.auth.trust, COL-112) instead of trusting
# ``X-Forwarded-Proto`` from any source. With no ``COLLAPSARR_TRUSTED_PROXIES``
# configured, that header no longer has any effect at all -- only the direct
# ASGI scheme does. Once the direct peer is on the allowlist, that proxy's
# ``X-Forwarded-Proto`` is honoured; an untrusted peer's is still ignored,
# exactly the same forgeability rule COL-113 applied to ``X-Forwarded-For``.


@contextmanager
def _client_for_peer_and_scheme(
    settings: Settings, host: str, *, scheme: str = "http"
) -> Iterator[TestClient]:
    """Like ``_client_for_peer`` but also controls the ASGI scheme TestClient
    reports: a ``https://testserver`` base URL makes ``request.scope["scheme"]
    == "https"``, so direct-HTTPS cases can be exercised alongside a chosen
    peer address."""
    app = create_app(settings=settings)
    with TestClient(
        app,
        base_url=f"{scheme}://testserver",
        client=(host, 51234),
        follow_redirects=False,
    ) as test_client:
        yield test_client


def _login_set_cookie(test_client: TestClient, **headers: str) -> str:
    """Seed a credential, log in, and return the ``Set-Cookie`` header value."""
    _seed_credential(test_client)
    login = test_client.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        headers=headers,
    )
    assert login.status_code == 200
    return str(login.headers["set-cookie"])


def test_secure_flag_absent_over_plain_http_with_no_trusted_proxies(
    settings: Settings,
) -> None:
    with _client_for_peer_and_scheme(settings, PUBLIC_HOST) as test_client:
        assert "secure" not in _login_set_cookie(test_client).lower()


def test_secure_flag_ignores_forwarded_proto_with_no_trusted_proxies(
    settings: Settings,
) -> None:
    """Closes the pre-COL-114 gap: X-Forwarded-Proto from any source used to
    be trusted unconditionally, regardless of any allowlist."""
    with _client_for_peer_and_scheme(settings, PUBLIC_HOST) as test_client:
        cookie = _login_set_cookie(test_client, **{"X-Forwarded-Proto": "https"})
        assert "secure" not in cookie.lower()


def test_secure_flag_set_for_forwarded_proto_from_a_trusted_proxy(tmp_path: Path) -> None:
    settings = _settings_with_trusted_proxies(tmp_path, f"{TRUSTED_PROXY_HOST}/32")
    with _client_for_peer_and_scheme(settings, TRUSTED_PROXY_HOST) as test_client:
        cookie = _login_set_cookie(test_client, **{"X-Forwarded-Proto": "https"})
        assert "secure" in cookie.lower()


def test_secure_flag_absent_for_spoofed_forwarded_proto_from_an_untrusted_peer(
    tmp_path: Path,
) -> None:
    """A peer not on the allowlist cannot force Secure via a forged header,
    even while the direct connection is plain HTTP."""
    settings = _settings_with_trusted_proxies(tmp_path, f"{TRUSTED_PROXY_HOST}/32")
    with _client_for_peer_and_scheme(settings, PUBLIC_HOST) as test_client:
        cookie = _login_set_cookie(test_client, **{"X-Forwarded-Proto": "https"})
        assert "secure" not in cookie.lower()


def test_secure_flag_set_for_direct_https_regardless_of_allowlist(tmp_path: Path) -> None:
    settings = _settings_with_trusted_proxies(tmp_path, "")
    with _client_for_peer_and_scheme(settings, PUBLIC_HOST, scheme="https") as test_client:
        assert "secure" in _login_set_cookie(test_client).lower()


# --- Basic auth method (COL-52) ------------------------------------------------
#
# Same credential core as Forms (verified via ``verify_auth_password``), a
# different transport: a browser-native ``WWW-Authenticate: Basic`` challenge
# instead of a redirect to ``/login``. These use the plain ``client``/
# ``noredirect_client`` fixtures (non-local peer, per the comment above) so
# local_bypass never masks the method's own behaviour.


def test_basic_method_challenges_an_unauthenticated_ui_request(
    noredirect_client: TestClient, session: Session
) -> None:
    _set_credential(session)
    update_global_settings(session, auth_method=AUTH_METHOD_BASIC)

    response = noredirect_client.get(UI_ROUTE)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].lower().startswith("basic")


def test_basic_method_grants_access_with_correct_credentials(
    noredirect_client: TestClient, session: Session
) -> None:
    _set_credential(session)
    update_global_settings(session, auth_method=AUTH_METHOD_BASIC)

    response = noredirect_client.get(UI_ROUTE, headers=_basic_header(USERNAME, PASSWORD))

    assert response.status_code == 404  # auth passed; no SPA mounted


def test_basic_method_rejects_incorrect_credentials(
    noredirect_client: TestClient, session: Session
) -> None:
    _set_credential(session)
    update_global_settings(session, auth_method=AUTH_METHOD_BASIC)

    response = noredirect_client.get(UI_ROUTE, headers=_basic_header(USERNAME, "wrong"))

    assert response.status_code == 401
    assert response.headers["www-authenticate"].lower().startswith("basic")


def test_basic_method_rejects_an_unknown_username(
    noredirect_client: TestClient, session: Session
) -> None:
    _set_credential(session)
    update_global_settings(session, auth_method=AUTH_METHOD_BASIC)

    response = noredirect_client.get(UI_ROUTE, headers=_basic_header("someone-else", PASSWORD))

    assert response.status_code == 401


def test_basic_method_mints_a_session_so_later_requests_need_no_header(
    noredirect_client: TestClient, session: Session
) -> None:
    _set_credential(session)
    update_global_settings(session, auth_method=AUTH_METHOD_BASIC)

    challenged = noredirect_client.get(UI_ROUTE, headers=_basic_header(USERNAME, PASSWORD))
    assert challenged.status_code == 404
    assert "collapsarr_session" in noredirect_client.cookies

    # The client's cookie jar now carries the session, so a follow-up request
    # with no Authorization header at all still passes.
    followup = noredirect_client.get(UI_ROUTE)
    assert followup.status_code == 404


def test_basic_method_leaves_api_session_or_key_behaviour_unchanged(
    client: TestClient, session: Session
) -> None:
    key = _set_credential(session)
    update_global_settings(session, auth_method=AUTH_METHOD_BASIC)

    # No session, no key: rejected exactly like under Forms.
    assert client.post(WEBHOOK_ROUTE, json={}).status_code == 401
    # A valid API key still passes, unaffected by the method choice.
    assert client.post(WEBHOOK_ROUTE, json={}, headers={"X-Api-Key": key}).status_code == 404


def test_basic_method_leaves_health_open(client: TestClient, session: Session) -> None:
    _set_credential(session)
    update_global_settings(session, auth_method=AUTH_METHOD_BASIC)

    assert client.get("/health").status_code == 200


def test_auth_method_is_switchable_via_the_settings_api(
    client: TestClient, session: Session
) -> None:
    key = _set_credential(session)

    response = client.put(
        "/api/settings", json={"auth_method": "basic"}, headers={"X-Api-Key": key}
    )

    assert response.status_code == 200
    assert response.json()["auth_method"] == "basic"
    assert get_global_settings(session).auth_method == AUTH_METHOD_BASIC


def test_auth_status_reports_the_active_method(client: TestClient, session: Session) -> None:
    _set_credential(session)
    update_global_settings(session, auth_method=AUTH_METHOD_BASIC)

    response = client.get("/api/auth/status")

    assert response.status_code == 200
    assert response.json()["auth_method"] == "basic"


# --- change password / log out everywhere (COL-55) ---------------------------


NEW_PASSWORD = "a whole new battery staple"


def test_change_password_rejects_the_wrong_current_password(
    client: TestClient, session: Session
) -> None:
    _set_credential(session)
    client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})

    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "not the password", "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 401
    # The stored credential is untouched: the original password still works.
    assert get_global_settings(session).auth_password_hash is not None
    login = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert login.status_code == 200


def test_change_password_with_the_correct_current_password_rotates_which_password_authenticates(
    client: TestClient, session: Session
) -> None:
    _set_credential(session)
    client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})

    response = client.post(
        "/api/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 200
    assert response.json()["authenticated"] is True

    # Old password no longer authenticates...
    old_login = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert old_login.status_code == 401

    # ...the new one does.
    new_login = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": NEW_PASSWORD}
    )
    assert new_login.status_code == 200


def test_change_password_before_a_credential_exists_is_conflict(
    client: TestClient, session: Session
) -> None:
    key = get_global_settings(session).api_key  # auto-generated on row creation

    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "x", "new_password": "y"},
        headers={"X-Api-Key": key},
    )

    assert response.status_code == 409


def test_change_password_requires_a_session_or_api_key(
    client: TestClient, session: Session
) -> None:
    _set_credential(session)

    response = client.post(
        "/api/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 401


def test_logout_everywhere_rotates_the_secret_and_invalidates_existing_sessions(
    noredirect_client: TestClient, session: Session
) -> None:
    _set_credential(session)
    original_secret = get_global_settings(session).session_secret

    login = noredirect_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    assert login.status_code == 200
    old_cookie = noredirect_client.cookies.get("collapsarr_session")
    assert old_cookie is not None
    # Logged in: UI route passes through.
    assert noredirect_client.get(UI_ROUTE).status_code == 404

    response = noredirect_client.post("/api/auth/logout-everywhere")

    assert response.status_code == 200
    assert response.json() == {
        "needs_setup": False,
        "authenticated": False,
        "auth_method": "forms",
    }
    # The secret changed in the DB...
    assert get_global_settings(session).session_secret != original_secret
    # ...the current browser's cookie is discarded in the same response...
    assert "expires=Thu, 01 Jan 1970" in response.headers["set-cookie"]
    # ...and this request's own UI access is already gone.
    assert noredirect_client.get(UI_ROUTE).status_code == 303

    # A different browser replaying the OLD (pre-rotation) cookie is rejected
    # too -- it fails to unsign against the rotated secret, not merely absent.
    noredirect_client.cookies.set("collapsarr_session", old_cookie)
    redirect = noredirect_client.get(UI_ROUTE)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/login"


def test_logout_everywhere_requires_a_session_or_api_key(
    client: TestClient, session: Session
) -> None:
    _set_credential(session)

    assert client.post("/api/auth/logout-everywhere").status_code == 401


def test_logout_everywhere_leaves_the_credential_itself_unchanged(
    noredirect_client: TestClient, session: Session
) -> None:
    """Rotating the secret is orthogonal to the password -- it stays valid."""
    _set_credential(session)
    noredirect_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )

    noredirect_client.post("/api/auth/logout-everywhere")

    relogin = noredirect_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    assert relogin.status_code == 200


# --- COLLAPSARR_URL_BASE reflected in redirects and cookie path (COL-117) -----
#
# UrlBaseMiddleware (COL-116) strips the configured prefix from the request
# path before enforce_auth_middleware/SessionMiddleware see it, so these
# tests send the *prefixed* path (as a reverse proxy would forward it) and
# assert the outbound Location/Set-Cookie headers carry the prefix back --
# the reverse of that inbound strip. Mirrors the settings-builder +
# client-builder split used above for the trusted-proxy tests
# (``_settings_with_trusted_proxies`` / ``_client_for_peer``).


def _settings_with_url_base(tmp_path: Path, url_base: str) -> Settings:
    """A ``Settings`` instance like the ``settings`` fixture's, but with
    ``COLLAPSARR_URL_BASE`` set (COL-116) -- for tests that need the
    configured prefix reflected in outbound redirect/cookie headers (COL-117)."""
    db_path = tmp_path / "collapsarr.db"
    return Settings(database_path=str(db_path), data_dir=str(tmp_path), url_base=url_base)


@contextmanager
def _url_base_client(settings: Settings) -> Iterator[TestClient]:
    """A no-follow-redirects TestClient built from ``settings`` (typically from
    ``_settings_with_url_base``), so the gate's ``303``/``Location`` and
    ``Set-Cookie`` headers are observable."""
    app = create_app(settings=settings)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def test_first_run_gate_redirect_carries_url_base_prefix(tmp_path: Path) -> None:
    with _url_base_client(_settings_with_url_base(tmp_path, URL_BASE)) as test_client:
        response = test_client.get(f"{URL_BASE}{UI_ROUTE}")

        assert response.status_code == 303
        assert response.headers["location"] == f"{URL_BASE}/setup"


def test_unauthenticated_login_redirect_carries_url_base_prefix(tmp_path: Path) -> None:
    with _url_base_client(_settings_with_url_base(tmp_path, URL_BASE)) as test_client:
        _seed_credential(test_client)

        response = test_client.get(f"{URL_BASE}{UI_ROUTE}")

        assert response.status_code == 303
        assert response.headers["location"] == f"{URL_BASE}/login"


def test_post_login_root_redirect_carries_url_base_prefix(tmp_path: Path) -> None:
    with _url_base_client(_settings_with_url_base(tmp_path, URL_BASE)) as test_client:
        _seed_credential(test_client)
        login = test_client.post(
            f"{URL_BASE}/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )
        assert login.status_code == 200

        # Already authenticated: visiting /login itself bounces to the app root.
        response = test_client.get(f"{URL_BASE}/login")

        assert response.status_code == 303
        assert response.headers["location"] == f"{URL_BASE}/"


def test_authenticated_setup_root_redirect_carries_url_base_prefix(tmp_path: Path) -> None:
    with _url_base_client(_settings_with_url_base(tmp_path, URL_BASE)) as test_client:
        _seed_credential(test_client)
        login = test_client.post(
            f"{URL_BASE}/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )
        assert login.status_code == 200

        # Already authenticated: visiting /setup itself bounces to the app root.
        response = test_client.get(f"{URL_BASE}/setup")

        assert response.status_code == 303
        assert response.headers["location"] == f"{URL_BASE}/"


def test_authenticated_setup_root_redirect_is_unprefixed_without_url_base(
    noredirect_client: TestClient, session: Session
) -> None:
    """No ``url_base`` configured: byte-for-byte identical to today."""
    _set_credential(session)
    noredirect_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )

    response = noredirect_client.get("/setup")

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_authenticated_login_root_redirect_is_unprefixed_without_url_base(
    noredirect_client: TestClient, session: Session
) -> None:
    """No ``url_base`` configured: byte-for-byte identical to today."""
    _set_credential(session)
    noredirect_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )

    response = noredirect_client.get("/login")

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_session_cookie_path_scoped_to_url_base_on_login(tmp_path: Path) -> None:
    with _url_base_client(_settings_with_url_base(tmp_path, URL_BASE)) as test_client:
        _seed_credential(test_client)

        login = test_client.post(
            f"{URL_BASE}/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )

        assert f"path={URL_BASE}/;" in login.headers["set-cookie"]


def test_session_cookie_path_scoped_to_url_base_on_logout(tmp_path: Path) -> None:
    with _url_base_client(_settings_with_url_base(tmp_path, URL_BASE)) as test_client:
        _seed_credential(test_client)
        test_client.post(
            f"{URL_BASE}/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )

        logout = test_client.post(f"{URL_BASE}/api/auth/logout")

        assert logout.status_code == 200
        assert f"path={URL_BASE}/;" in logout.headers["set-cookie"]


def test_setup_redirect_is_unprefixed_without_url_base(
    noredirect_client: TestClient,
) -> None:
    """No ``url_base`` configured: byte-for-byte identical to today."""
    response = noredirect_client.get(UI_ROUTE)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_session_cookie_path_defaults_to_root_without_url_base(
    noredirect_client: TestClient, session: Session
) -> None:
    """No ``url_base`` configured: cookie path stays the unscoped root."""
    _set_credential(session)

    login = noredirect_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )

    assert "path=/;" in login.headers["set-cookie"]

    logout = noredirect_client.post("/api/auth/logout")

    assert "path=/;" in logout.headers["set-cookie"]
