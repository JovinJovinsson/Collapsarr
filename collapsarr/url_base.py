"""Strip-prefix ASGI middleware for ``COLLAPSARR_URL_BASE`` (COL-116).

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
