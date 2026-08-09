"""``COLLAPSARR_URL_BASE`` support: the strip-prefix ASGI middleware (COL-116)
and the outbound helpers that re-add the prefix (COL-117).

:class:`UrlBaseMiddleware` handles the *inbound* side -- stripping the
configured prefix from an incoming request path. :func:`external_path` and
:func:`cookie_path` handle the *outbound* side -- re-adding it to a redirect
``Location`` header or a session cookie's ``path`` attribute, both of which
are sent to the browser and so need the full external path/scope, not the
already-stripped internal one the rest of the app sees. See
:mod:`collapsarr.auth.enforcement` and :mod:`collapsarr.auth.session` for
the call sites.

See ``docs/adr/0004-url-base-strip-middleware-not-root-path-flag.md`` for the
design rationale: the reverse proxy passes the full, unstripped external path
through (no proxy-side rewrite rule required -- a plain ``proxy_pass``-style
config at Collapsarr "just works"), and Collapsarr itself recognizes and
strips its own configured prefix, the same pattern ASP.NET Core's
``UsePathBase`` implements internally. This is deliberately *not* the same
contract as uvicorn's ``--root-path`` flag, which assumes the proxy already
stripped the prefix.

:class:`UrlBaseMiddleware` is modelled on
:class:`collapsarr.auth.session.SessionMiddleware`'s raw-ASGI
``__call__(scope, receive, send)`` style -- the closest existing template for
a small, dependency-free middleware that needs to rewrite ``scope`` before
routing sees the request (a ``BaseHTTPMiddleware``/``@app.middleware("http")``
callback runs too late for that: it observes the already-routed request).

When ``url_base`` is empty (the default), the middleware is a pure pass-
through -- every existing route's behaviour is byte-for-byte identical to
today. When configured, a request whose path starts with the prefix has the
prefix stripped from ``scope["path"]`` (for internal routing) and appended to
``scope["root_path"]`` (feeding Starlette's own URL generation -- ``/docs``,
``/openapi.json``, ``request.url_for``). A request that arrives *without* the
prefix passes through unchanged rather than being rejected, so e.g. a Docker
healthcheck hitting ``/health`` on the bound port directly (bypassing the
proxy) keeps working.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send


def external_path(url_base: str, path: str) -> str:
    """Re-add ``url_base`` to an internal ``path`` for an outbound URL (COL-117).

    The reverse of the strip :class:`UrlBaseMiddleware` performs on the way
    in: a redirect ``Location`` header is sent to the browser, which needs
    the full external path -- including the reverse-proxy prefix -- not the
    already-stripped internal ``path`` the rest of the app sees by the time
    it builds the redirect. A no-op when ``url_base`` is empty (the
    default), so unconfigured installs keep today's unprefixed redirects.
    """
    return f"{url_base}{path}" if url_base else path


def cookie_path(url_base: str) -> str:
    """The session cookie's ``path`` attribute, scoped to ``url_base`` (COL-117).

    A cookie scoped to the bare root (``path=/``) would still be sent on
    requests below the reverse-proxy prefix that this Collapsarr instance
    never actually serves at (it only answers under ``<url_base>/...``);
    scoping the cookie to ``<url_base>/`` matches its exposure to the app's
    real external surface. Empty ``url_base`` falls back to ``/`` -- today's
    behaviour, unchanged.
    """
    return f"{url_base}/" if url_base else "/"


class UrlBaseMiddleware:
    """Strips a configured ``url_base`` prefix from incoming request paths.

    Must be registered as the **outermost** middleware (last call to
    ``app.add_middleware`` -- Starlette's stack treats the most-recently-added
    middleware as outermost/first-to-run) so it rewrites ``scope`` before any
    other middleware or routing logic sees the request. See
    :func:`collapsarr.main.create_app`.
    """

    def __init__(self, app: ASGIApp, url_base: str) -> None:
        self.app = app
        self.url_base = url_base

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.url_base or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if path == self.url_base:
            stripped = "/"
        elif path.startswith(self.url_base + "/"):
            stripped = path[len(self.url_base) :]
        else:
            # No prefix match -- pass through unchanged (ADR-0004): this is
            # deliberate, not a rejection, so unprefixed/direct traffic (e.g.
            # a Docker healthcheck) keeps working.
            await self.app(scope, receive, send)
            return

        scope = dict(scope)
        scope["path"] = stripped
        scope["root_path"] = scope.get("root_path", "") + self.url_base
        await self.app(scope, receive, send)
