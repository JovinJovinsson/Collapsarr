"""Serving the bundled single-page frontend (COL-40).

The Vite/React UI is built separately (``npm run build`` in ``frontend/``) into
``frontend/dist`` and bundled into the wheel as package data under
``collapsarr/static`` (see the hatchling ``force-include`` in
``pyproject.toml``). At runtime FastAPI serves that directory as a single-page
app: static assets are served directly and any unmatched path falls back to
``index.html`` so the client-side router can handle it.

When the static directory is absent -- e.g. a source checkout where the
frontend has not been built -- serving is skipped and only the JSON API is
exposed. The ``/api`` and ``/health`` routes are always registered before the
SPA mount, so they take precedence over the catch-all.

Reverse-proxy subpath support (COL-118): when ``COLLAPSARR_URL_BASE`` is set,
the served ``index.html`` is precomputed **once at startup** with an inline
``<script>`` exposing the configured prefix as ``window.__COLLAPSARR_URL_BASE__``
before the app bundle loads. The frontend reads that global at runtime to
prefix its API calls, browser navigation, and redirects (see
``frontend/src/runtime/urlBase.ts``). Static assets are already relative
(Vite ``base: "./"``), so they need no injection. When ``url_base`` is empty
(the default) nothing is injected and ``index.html`` is served byte-for-byte
as it is on disk, exactly as before this change.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

# The document paths StaticFiles resolves to ``index.html``: a request for the
# mount root normalizes to ``"."`` (see ``StaticFiles.get_path``), and a direct
# ``/index.html`` normalizes to ``"index.html"``.
_INDEX_PATHS = {".", "index.html"}


def get_frontend_dir() -> Path:
    """Absolute path to the bundled frontend assets inside the package."""
    return Path(__file__).parent / "static"


def _inject_url_base(html: str, url_base: str) -> str:
    """Insert the runtime ``url_base`` global into an ``index.html`` document.

    Emits ``<script>window.__COLLAPSARR_URL_BASE__ = "<url_base>";</script>``
    *before* the app bundle loads so the frontend can read the prefix as it
    boots. Inserts before the first ``<script`` tag (Vite's built document
    hoists the module bundle into ``<head>``, so this lands the classic inline
    global ahead of it in source order); falls back to just before ``</head>``,
    then to prepending, so a document shaped differently still gets the global.
    ``url_base`` is a server-validated path prefix (leading slash, no trailing
    slash -- see ``Settings._normalize_url_base``), not user input, so it is
    embedded directly.
    """
    script = f'<script>window.__COLLAPSARR_URL_BASE__ = "{url_base}";</script>'
    lowered = html.lower()
    script_open = lowered.find("<script")
    if script_open != -1:
        return html[:script_open] + script + html[script_open:]
    head_close = lowered.find("</head>")
    if head_close != -1:
        return html[:head_close] + script + html[head_close:]
    return script + html


class SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to ``index.html`` for unmatched paths.

    A single-page app owns its own routing, so a request for a client-side
    route (with no matching file on disk) must return ``index.html`` rather
    than a 404, letting the browser-side router take over.

    When ``url_base`` is configured (COL-118) the served ``index.html`` is
    rendered **once at construction** (not per-request) with the runtime
    ``window.__COLLAPSARR_URL_BASE__`` global injected, and that precomputed
    document is returned for every request that would otherwise serve
    ``index.html`` (the mount root, a direct ``/index.html``, and the SPA
    fallback). When ``url_base`` is empty the injection is skipped entirely and
    serving falls through to the on-disk file unchanged.
    """

    def __init__(self, *args: object, url_base: str = "", **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._injected_index: bytes | None = self._precompute_index(url_base)

    def _precompute_index(self, url_base: str) -> bytes | None:
        """Render the injected ``index.html`` once, or ``None`` when no prefix.

        Returning ``None`` for an empty ``url_base`` keeps the unconfigured
        path byte-for-byte identical to today: ``get_response`` never
        substitutes, so the on-disk file is served with its usual
        ETag/Last-Modified headers.
        """
        if not url_base:
            return None
        # ``self.directory`` is always set here: ``mount_frontend`` only
        # constructs this class after confirming the directory exists.
        assert self.directory is not None
        index_path = Path(self.directory) / "index.html"
        raw = index_path.read_text(encoding="utf-8")
        return _inject_url_base(raw, url_base).encode("utf-8")

    def _index_response(self) -> Response:
        """A fresh response wrapping the precomputed injected ``index.html``."""
        assert self._injected_index is not None
        return HTMLResponse(self._injected_index)

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404:
                if self._injected_index is not None:
                    return self._index_response()
                return await super().get_response("index.html", scope)
            raise
        if self._injected_index is not None and path in _INDEX_PATHS:
            return self._index_response()
        return response


def mount_frontend(
    app: FastAPI, static_dir: Path | None = None, url_base: str = ""
) -> bool:
    """Mount the SPA at ``/`` if its assets are present.

    Returns ``True`` when the frontend was mounted, ``False`` when the assets
    directory does not exist (so the app still boots and serves the API from a
    source checkout without a built frontend). Must be called after all API
    routers are registered so the catch-all mount does not shadow them.

    ``url_base`` (COL-118) is the configured reverse-proxy prefix
    (``Settings.url_base``): when non-empty it is injected into the served
    ``index.html`` as the runtime ``window.__COLLAPSARR_URL_BASE__`` global.
    Empty (the default) serves ``index.html`` unchanged.
    """
    directory = static_dir or get_frontend_dir()
    if not directory.is_dir():
        return False
    app.mount(
        "/",
        SPAStaticFiles(directory=directory, html=True, url_base=url_base),
        name="frontend",
    )
    return True
