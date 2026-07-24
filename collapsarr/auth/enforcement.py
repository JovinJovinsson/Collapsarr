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

Classification (:func:`_client_is_local`) reads only the literal peer address
the ASGI server accepted the connection from (``request.client``) -- it never
parses ``X-Forwarded-For`` or any other client-suppliable header, which any
caller could forge. The consequence: behind a reverse proxy, the proxy's own
address is what gets classified, not its upstream client's -- an install
behind a reverse proxy should set ``auth_required="enabled"`` until a later
ticket adds trusted-proxy support (see the README's Authentication section).

The Basic auth method (COL-52) slots in at ``is_authenticated`` without
disturbing this routing.
"""

from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from ..settings.models import AUTH_REQUIRED_LOCAL_BYPASS
from ..settings.service import get_global_settings
from .session import is_authenticated

API_KEY_HEADER = "X-Api-Key"
"""Request header carrying the API key (Sonarr/Radarr convention)."""

API_KEY_QUERY = "apikey"
"""Query-parameter fallback for callers that cannot set a custom header."""

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


def _client_is_local(request: Request) -> bool:
    """Whether the request's direct TCP peer is loopback or a private-range address.

    Reads ``request.client`` -- the literal address the ASGI server accepted
    the connection from -- and nothing else. In particular this deliberately
    does **not** consult ``X-Forwarded-For`` (or any other header): those are
    supplied by the client and trivially spoofable, so trusting them here
    would let any external caller claim to be local and bypass auth entirely.
    The tradeoff (documented in the module docstring and the README) is that
    an install behind a reverse proxy sees the proxy's own peer address, not
    its upstream client's -- trusted-proxy support is a later stub.
    """
    client = request.client
    if client is None:
        return False
    try:
        address = ipaddress.ip_address(client.host)
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

    if path == HEALTH_PATH or path in OPEN_API_PATHS or _is_static_asset(path):
        return await call_next(request)

    session_factory = request.app.state.session_factory
    with session_factory() as session:
        settings = get_global_settings(session)
        credential_set = settings.auth_username is not None
        expected_key = settings.api_key
        auth_required = settings.auth_required

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
        return RedirectResponse(url=SETUP_PATH, status_code=_REDIRECT_STATUS)

    if path == SETUP_PATH:
        # Credential already exists -- setup is done.
        target = APP_ROOT if authed else LOGIN_PATH
        return RedirectResponse(url=target, status_code=_REDIRECT_STATUS)

    if path == LOGIN_PATH:
        if authed:
            return RedirectResponse(url=APP_ROOT, status_code=_REDIRECT_STATUS)
        return await call_next(request)

    if authed:
        return await call_next(request)
    return RedirectResponse(url=LOGIN_PATH, status_code=_REDIRECT_STATUS)
