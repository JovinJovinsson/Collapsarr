"""Request-gating middleware for the whole app surface (COL-50).

This is the successor to the old opt-in ``api_key_middleware`` (COL-26). Auth is
now **unconditional**, not gated on a ``ui_auth_enabled`` toggle: the app still
launches with zero config, but the UI cannot be used until a credential exists,
and once it does, every UI route needs a session.

Routing decision, per request:

* ``/health`` -- always open (the Sonarr/Radarr ``/ping``-style liveness probe).
* Static bundle assets (any path whose final segment has a file extension, e.g.
  ``/assets/index-abc.js``, ``/favicon.svg``) -- always open, so the ``/setup``
  and ``/login`` pages can load their JS/CSS before a session exists. These are
  the SPA's public bundle, not sensitive data (which lives behind ``/api``).
* ``/api/auth/{setup,login,status}`` -- always open, so the first-run and login
  flows can run with no session yet. (``/api/auth/logout`` is *not* here: it
  needs a session, and enforcement below supplies that.)
* ``/api/*`` -- open with a valid session **or** the API key (``X-Api-Key``
  header or ``?apikey=`` query param, preserving the Sonarr/Radarr webhook
  convention); otherwise ``401``.
* Everything else is a browser (SPA navigation) route:
    * While **no credential exists** -- the first-run gate -- only ``/setup`` is
      served; every other route ``303``-redirects to ``/setup``.
    * Once a **credential exists**, a valid session serves the route; otherwise
      it ``303``-redirects to ``/login`` (and ``/login``/``/setup`` themselves
      redirect a logged-in user back into the app, and pre-credential ``/login``
      redirects to ``/setup``).

Required-mode (COL-51): the routing table above is what ``auth_required=
"enabled"`` gets. When ``auth_required="local_bypass"`` (the default -- see
:class:`collapsarr.settings.models.GlobalSettings`) *and* the caller's direct
peer address is loopback or a private range, every check above is skipped --
the request passes straight through, same as ``/health``. A caller whose peer
address is routable/public is challenged exactly per the table, regardless of
mode. This makes a LAN/localhost self-hoster's install frictionless (no
setup, no login, no API key) while any routable-address client -- including
one pretending to be local -- must still authenticate.

Classification (:func:`_client_is_local`) resolves the address via
:func:`collapsarr.auth.trust.resolve_client_address` (COL-112/COL-113), not
the raw ASGI peer: by default (no ``COLLAPSARR_TRUSTED_PROXIES`` configured)
that is exactly the literal peer address the server accepted the connection
from, same as before -- ``X-Forwarded-For`` is never consulted. Once an
install lists its reverse proxy's address in ``COLLAPSARR_TRUSTED_PROXIES``,
a request whose *direct* peer is that trusted proxy is classified on the
rightmost ``X-Forwarded-For`` entry instead (the proxy's own view of its
immediate client), so the real upstream client -- not the proxy -- is what
determines local-vs-routable. A peer that is not on the allowlist is still
classified by its own direct address, and any ``X-Forwarded-For`` it presents
is ignored -- exactly as forgeable, and as ignored, as before.

The Basic auth method (COL-52) slots in at the browser-route branch below:
when ``auth_method="basic"``, an unauthenticated browser request gets a ``401``
+ ``WWW-Authenticate: Basic`` challenge instead of a redirect to ``/login``; a
valid ``Authorization: Basic`` header verified against the same credential
(:func:`collapsarr.settings.service.verify_auth_password` -- COL-49's single
operator credential, shared with Forms) mints a normal session
(:func:`collapsarr.auth.session.log_in`), so every check *after* that --
including ``/api``'s session-or-key rule -- is untouched. Only the initial UI
challenge's transport differs between the two methods.
"""

from __future__ import annotations

import base64
import ipaddress
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from ..settings.models import AUTH_METHOD_BASIC, AUTH_REQUIRED_LOCAL_BYPASS
from ..settings.service import get_global_settings, verify_auth_password
from ..url_base import external_path
from .session import is_authenticated, log_in
from .trust import resolve_client_address

API_KEY_HEADER = "X-Api-Key"
"""Request header carrying the API key (Sonarr/Radarr convention)."""

API_KEY_QUERY = "apikey"
"""Query-parameter fallback for callers that cannot set a custom header."""

AUTHORIZATION_HEADER = "authorization"
BASIC_SCHEME_PREFIX = "basic "
BASIC_REALM = "Collapsarr"
"""``WWW-Authenticate`` realm presented in the Basic auth challenge."""

HEALTH_PATH = "/health"
SETUP_PATH = "/setup"
LOGIN_PATH = "/login"
APP_ROOT = "/"
API_PREFIX = "/api"

OPEN_API_PATHS = frozenset({"/api/auth/setup", "/api/auth/login", "/api/auth/status"})
"""``/api`` endpoints reachable before any session/credential exists."""

_REDIRECT_STATUS = 303
"""``See Other``: gate redirects are GET navigations, so switch method to GET."""


def _extract_key(request: Request) -> str | None:
    """Return the presented API key from the header, then query string, if any."""
    header = request.headers.get(API_KEY_HEADER)
    if header:
        return header
    return request.query_params.get(API_KEY_QUERY) or None


def _parse_basic_credentials(request: Request) -> tuple[str, str] | None:
    """Decode an ``Authorization: Basic <base64>`` header into
    ``(username, password)``, or ``None`` if it is absent or malformed."""
    header = request.headers.get(AUTHORIZATION_HEADER)
    if header is None or not header.lower().startswith(BASIC_SCHEME_PREFIX):
        return None
    try:
        decoded = base64.b64decode(header[len(BASIC_SCHEME_PREFIX) :].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    return username, password


def _prefixed_redirect(url_base: str, path: str) -> RedirectResponse:
    """A gate ``RedirectResponse`` to ``path``, with ``url_base`` re-added (COL-117).

    Every gate redirect below goes through this instead of building
    ``RedirectResponse`` directly, so the ``Location`` header is always the
    correct external URL for a browser behind a reverse proxy -- see
    :func:`collapsarr.url_base.external_path`.
    """
    return RedirectResponse(url=external_path(url_base, path), status_code=_REDIRECT_STATUS)


def _basic_challenge() -> Response:
    """The ``401`` + ``WWW-Authenticate: Basic`` response for an
    unauthenticated request under the Basic auth method (COL-52)."""
    return JSONResponse(
        status_code=401,
        content={"detail": "Basic authentication required."},
        headers={"WWW-Authenticate": f'Basic realm="{BASIC_REALM}"'},
    )


def _client_is_local(request: Request) -> bool:
    """Whether the request's resolved client address is loopback or private-range.

    The address comes from :func:`~collapsarr.auth.trust.resolve_client_address`
    (COL-112), not straight from ``request.client``: with no trusted proxy
    configured (the default) that is the same literal peer address the ASGI
    server accepted the connection from, so behavior is unchanged. Only when
    the *direct* peer is on the ``COLLAPSARR_TRUSTED_PROXIES`` allowlist does
    the resolved address instead reflect the rightmost ``X-Forwarded-For``
    entry -- an untrusted peer's forwarded header is never consulted, so it
    cannot spoof its way into a local classification.
    """
    address_str = resolve_client_address(request)
    if address_str is None:
        return False
    try:
        address = ipaddress.ip_address(address_str)
    except ValueError:
        # Not a literal IP address (seen in some non-network test harnesses) --
        # treat conservatively as not local.
        return False
    return address.is_loopback or address.is_private


def _is_static_asset(path: str) -> bool:
    """Whether ``path`` names a bundled static file (has a file extension).

    SPA navigation routes are extensionless (``/``, ``/wanted``, ``/setup``);
    built assets carry a hashed filename with an extension
    (``/assets/index-abc.js``, ``/favicon.svg``). Serving the public JS/CSS
    bundle unauthenticated is what lets the login/setup pages render at all.
    """
    return "." in path.rsplit("/", 1)[-1]


async def enforce_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Gate every request per the routing table in the module docstring."""
    path = request.url.path

    # The static-asset bypass is for the SPA's public JS/CSS bundle only; it must
    # never open an ``/api`` route. Without the ``API_PREFIX`` guard, any ``/api``
    # path whose final segment has a file extension (e.g. a backup id ending in
    # ``.zip`` -- COL-65's ``DELETE /api/system/backup/{type}/{file}.zip``) would
    # be misread as a static asset and skip the ``/api`` session/key gate below.
    if path == HEALTH_PATH or path in OPEN_API_PATHS or (
        not path.startswith(API_PREFIX) and _is_static_asset(path)
    ):
        return await call_next(request)

    # Re-added to any redirect Location header below (COL-117): by the time
    # this middleware runs, UrlBaseMiddleware (COL-116) has already stripped
    # the configured prefix from `path`, but a Location header is an
    # outbound URL sent to the browser, which needs the full external path
    # -- prefix included. Empty when unconfigured, so redirects stay
    # unprefixed exactly as before.
    url_base: str = request.app.state.settings.url_base

    session_factory = request.app.state.session_factory
    with session_factory() as session:
        settings = get_global_settings(session)
        credential_set = settings.auth_username is not None
        expected_key = settings.api_key
        auth_required = settings.auth_required
        auth_method = settings.auth_method
        auth_username = settings.auth_username

    if auth_required == AUTH_REQUIRED_LOCAL_BYPASS and _client_is_local(request):
        # local_bypass + a loopback/private-range peer: trust the network,
        # skip every check below (first-run gate, session, API key) same as
        # /health. A non-local peer falls through to the normal routing.
        return await call_next(request)

    authed = is_authenticated(request)

    if path.startswith(API_PREFIX):
        if authed:
            return await call_next(request)
        provided = _extract_key(request)
        if provided is not None and secrets.compare_digest(provided, expected_key):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key."},
        )

    # --- Browser (SPA navigation) routes -------------------------------------
    if not credential_set:
        # First-run gate: only the setup page is reachable.
        if path == SETUP_PATH:
            return await call_next(request)
        return _prefixed_redirect(url_base, SETUP_PATH)
    assert auth_username is not None  # credential_set is True past this point

    if path == SETUP_PATH:
        # Credential already exists -- setup is done.
        if authed:
            return _prefixed_redirect(url_base, APP_ROOT)
        if auth_method == AUTH_METHOD_BASIC:
            return _basic_challenge()
        return _prefixed_redirect(url_base, LOGIN_PATH)

    if auth_method == AUTH_METHOD_BASIC:
        # No Forms /login page under this method -- every other browser route
        # (including /login itself, if visited directly) is challenged or
        # passed the same way.
        if authed:
            return await call_next(request)
        creds = _parse_basic_credentials(request)
        if creds is not None and creds[0] == auth_username:
            with session_factory() as session:
                password_ok = verify_auth_password(session, creds[1])
            if password_ok:
                # Same credential core as Forms (COL-49) -- mint a session so
                # this browser's subsequent /api calls keep working the
                # unchanged session-or-key way; only the initial UI challenge
                # differs.
                log_in(request, auth_username, remember=False)
                return await call_next(request)
        return _basic_challenge()

    if path == LOGIN_PATH:
        if authed:
            return _prefixed_redirect(url_base, APP_ROOT)
        return await call_next(request)

    if authed:
        return await call_next(request)
    return _prefixed_redirect(url_base, LOGIN_PATH)
