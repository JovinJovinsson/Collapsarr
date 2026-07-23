"""API-key authentication for the HTTP API (COL-26).

When :attr:`~collapsarr.settings.models.GlobalSettings.ui_auth_enabled` is on,
every request under the ``/api`` prefix must present the instance's
auto-generated API key, matching the Sonarr/Radarr convention: the
``X-Api-Key`` request header (or, as a fallback for callers that can only set a
query string -- e.g. a Sonarr/Radarr webhook URL -- an ``apikey`` query
parameter). The key itself lives on the persisted
:class:`~collapsarr.settings.models.GlobalSettings` row and is minted on first
run (see :func:`collapsarr.settings.models.generate_api_key`), so it is
retrievable and rotatable through the same Settings surface as every other
setting.

``ui_auth_enabled`` defaults to ``False``: a fresh install has no way to learn
its auto-generated key without an already-authenticated request, so
enforcement is opt-in. Flip it on once the key has been copied out of
Settings.

Non-``/api`` routes (the ``/health`` liveness probe and the interactive API
docs) are intentionally left open -- ``/health`` is an unauthenticated probe by
the same convention Sonarr/Radarr's ``/ping`` follows.

The check is wired as HTTP middleware by
:func:`collapsarr.main.create_app`; it reads the expected key from the
request-scoped session factory on ``app.state`` and compares it in constant
time.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from .settings.service import get_global_settings

API_KEY_HEADER = "X-Api-Key"
"""Request header carrying the API key (Sonarr/Radarr convention)."""

API_KEY_QUERY = "apikey"
"""Query-parameter fallback for callers that cannot set a custom header."""

PROTECTED_PREFIX = "/api"
"""Only routes under this path prefix require the API key."""


def _extract_key(request: Request) -> str | None:
    """Return the presented API key from the header, then query string, if any."""
    header = request.headers.get(API_KEY_HEADER)
    if header:
        return header
    return request.query_params.get(API_KEY_QUERY) or None


def _requires_api_key(path: str) -> bool:
    """Whether ``path`` is a protected API route (i.e. under ``/api``)."""
    return path.startswith(PROTECTED_PREFIX)


async def api_key_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Reject ``/api`` requests lacking a valid API key with ``401``.

    Enforcement is gated on :attr:`~collapsarr.settings.models.GlobalSettings.ui_auth_enabled`
    (default ``False``), so a fresh install -- which has no way to learn its
    auto-generated key without first calling ``/api`` -- works out of the box:
    every route, including ``GET /api/settings`` where the key is displayed, is
    open until the user opts into requiring it. Non-API routes always pass
    straight through. When enabled, the expected key is read from the
    singleton settings row and compared to the presented one in constant time;
    a missing or mismatched key yields a ``401`` before the route handler ever
    runs.
    """
    if not _requires_api_key(request.url.path):
        return await call_next(request)

    session_factory: sessionmaker[Session] = request.app.state.session_factory
    with session_factory() as session:
        global_settings = get_global_settings(session)
        if not global_settings.ui_auth_enabled:
            return await call_next(request)
        expected: str = global_settings.api_key

    provided = _extract_key(request)
    if provided is None or not secrets.compare_digest(provided, expected):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key."},
        )

    return await call_next(request)
