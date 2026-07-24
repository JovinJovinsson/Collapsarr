"""Signed-cookie session support for the Forms auth flow (COL-50).

This is the session layer the enforcement middleware and the ``/api/auth``
routes build on. It is modelled directly on Starlette's own
:class:`starlette.middleware.sessions.SessionMiddleware` -- same
``itsdangerous`` ``TimestampSigner`` signing, same base64-JSON cookie payload,
and the same :class:`~starlette.middleware.sessions.Session` scope object -- but
differs in two deliberate ways this ticket needs and the vanilla middleware
cannot express:

* **Per-install signing key.** The key is the install's ``session_secret`` from
  the persisted :class:`~collapsarr.settings.models.GlobalSettings` row (minted
  once on first run, stable thereafter), read lazily from the DB on first use
  and cached on ``app.state``. Vanilla ``SessionMiddleware`` bakes a static key
  in at construction time, which we don't have -- the DB isn't open yet when
  :func:`collapsarr.main.create_app` wires middleware.
* **Per-response cookie lifetime and Secure flag.** A "remember me" login yields
  a long-lived (:data:`REMEMBER_MAX_AGE`) cookie; an unchecked login yields a
  browser-session cookie (no ``Max-Age``). The ``Secure`` attribute is set only
  when the request arrived over TLS (direct HTTPS or an ``X-Forwarded-Proto:
  https`` from a reverse proxy). Vanilla ``SessionMiddleware`` fixes both at
  construction.

The ``local_bypass`` required-mode (COL-51) and the Basic auth method (COL-52)
are intentionally *not* handled here -- they slot into the enforcement layer,
which decides *whether* a session is required; this module only mints and reads
the session itself.
"""

from __future__ import annotations

import json
from base64 import b64decode, b64encode

import itsdangerous
from fastapi import Request
from itsdangerous.exc import BadSignature
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import Session
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..settings.service import get_global_settings

SESSION_COOKIE = "collapsarr_session"
"""Name of the signed session cookie."""

SESSION_USER_KEY = "user"
"""Session key holding the authenticated operator's username."""

SESSION_PERSIST_KEY = "_persist"
"""Session key recording whether "remember me" was chosen (long-lived cookie)."""

REMEMBER_MAX_AGE = 30 * 24 * 60 * 60
"""Lifetime, in seconds, of a "remember me" cookie (30 days). Also the signer's
maximum unsign window, so a browser-session cookie still validates while the
browser is open."""

_SESSION_SECRET_STATE = "_session_secret"
"""``app.state`` attribute caching the resolved signing secret."""


def is_authenticated(request: Request) -> bool:
    """Whether the request carries a valid, logged-in session."""
    return bool(request.session.get(SESSION_USER_KEY))


def log_in(request: Request, username: str, *, remember: bool) -> None:
    """Mark the session as authenticated for ``username``.

    ``remember`` selects the cookie lifetime: ``True`` persists it for
    :data:`REMEMBER_MAX_AGE`, ``False`` makes it a browser-session cookie.
    """
    request.session[SESSION_USER_KEY] = username
    request.session[SESSION_PERSIST_KEY] = remember


def log_out(request: Request) -> None:
    """Clear the session, discarding the cookie on the response."""
    request.session.clear()


def _security_flags(secure: bool) -> str:
    """Cookie attributes: always ``HttpOnly`` + ``SameSite=Lax``; ``Secure`` on TLS."""
    flags = "httponly; samesite=lax"
    if secure:
        flags += "; secure"
    return flags


def _is_secure(connection: HTTPConnection) -> bool:
    """Whether the request arrived over TLS (direct or via a trusted proxy header)."""
    if connection.scope.get("scheme") == "https":
        return True
    forwarded = connection.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


def sync_cached_secret(app: object, secret: str) -> None:
    """Push a freshly rotated ``session_secret`` into the ``app.state`` cache.

    :func:`_get_signer` caches the secret it reads from the DB on ``app.state``
    for the life of the process, so writing a new value to the DB alone (see
    :func:`collapsarr.settings.service.rotate_session_secret`) would not affect
    an already-running process's signing/verification until it restarted --
    every cookie already-issued *and* every cookie the still-running process
    would go on to mint would keep using the stale cached secret. "Log out
    everywhere" (COL-55) needs the invalidation to take effect immediately, so
    the route handler that rotates the secret calls this in the same request
    to update the cache the next request's :class:`SessionMiddleware` pass
    will read.
    """
    setattr(app.state, _SESSION_SECRET_STATE, secret)  # type: ignore[attr-defined]


def _get_signer(app: object) -> itsdangerous.TimestampSigner:
    """Build (and cache) the cookie signer keyed on the install's session secret.

    The secret is read once from the singleton settings row and cached on
    ``app.state`` so subsequent requests don't re-hit the DB. It is stable for
    the life of the process (and across restarts), so signatures stay valid.
    """
    secret = getattr(app.state, _SESSION_SECRET_STATE, None)  # type: ignore[attr-defined]
    if secret is None:
        session_factory = app.state.session_factory  # type: ignore[attr-defined]
        with session_factory() as session:
            secret = get_global_settings(session).session_secret
        setattr(app.state, _SESSION_SECRET_STATE, secret)  # type: ignore[attr-defined]
    return itsdangerous.TimestampSigner(str(secret))


class SessionMiddleware:
    """Signed-cookie session middleware; see the module docstring for how it
    differs from Starlette's stock ``SessionMiddleware``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        signer = _get_signer(scope["app"])
        connection = HTTPConnection(scope)
        initial_session_was_empty = True

        if SESSION_COOKIE in connection.cookies:
            data = connection.cookies[SESSION_COOKIE].encode("utf-8")
            try:
                unsigned = signer.unsign(data, max_age=REMEMBER_MAX_AGE)
                scope["session"] = Session(json.loads(b64decode(unsigned)))
                initial_session_was_empty = False
            except BadSignature:
                scope["session"] = Session()
        else:
            scope["session"] = Session()

        secure = _is_secure(connection)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                session: Session = scope["session"]
                headers = MutableHeaders(scope=message)
                if session.accessed:
                    headers.add_vary_header("Cookie")
                if session.modified and session:
                    persist = bool(session.get(SESSION_PERSIST_KEY))
                    payload = b64encode(json.dumps(session).encode("utf-8"))
                    signed = signer.sign(payload).decode("utf-8")
                    max_age = f"Max-Age={REMEMBER_MAX_AGE}; " if persist else ""
                    header_value = (
                        f"{SESSION_COOKIE}={signed}; path=/; "
                        f"{max_age}{_security_flags(secure)}"
                    )
                    headers.append("Set-Cookie", header_value)
                elif session.modified and not initial_session_was_empty:
                    header_value = (
                        f"{SESSION_COOKIE}=null; path=/; "
                        f"expires=Thu, 01 Jan 1970 00:00:00 GMT; "
                        f"{_security_flags(secure)}"
                    )
                    headers.append("Set-Cookie", header_value)
            await send(message)

        await self.app(scope, receive, send_wrapper)
