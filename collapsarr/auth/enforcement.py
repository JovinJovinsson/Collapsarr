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

Required-mode is fixed to ``enabled`` here. The ``local_bypass`` mode (COL-51)
and the Basic auth method (COL-52) slot in at the marked seam below without
disturbing this happy path.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

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

    # Seam for COL-51 (local_bypass): required-mode is fixed to ``enabled`` in
    # this slice, so a session is always required. A later ticket decides here
    # whether a local-network caller may skip the session check below.
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
