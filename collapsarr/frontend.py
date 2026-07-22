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
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


def get_frontend_dir() -> Path:
    """Absolute path to the bundled frontend assets inside the package."""
    return Path(__file__).parent / "static"


class SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to ``index.html`` for unmatched paths.

    A single-page app owns its own routing, so a request for a client-side
    route (with no matching file on disk) must return ``index.html`` rather
    than a 404, letting the browser-side router take over.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def mount_frontend(app: FastAPI, static_dir: Path | None = None) -> bool:
    """Mount the SPA at ``/`` if its assets are present.

    Returns ``True`` when the frontend was mounted, ``False`` when the assets
    directory does not exist (so the app still boots and serves the API from a
    source checkout without a built frontend). Must be called after all API
    routers are registered so the catch-all mount does not shadow them.
    """
    directory = static_dir or get_frontend_dir()
    if not directory.is_dir():
        return False
    app.mount("/", SPAStaticFiles(directory=directory, html=True), name="frontend")
    return True
