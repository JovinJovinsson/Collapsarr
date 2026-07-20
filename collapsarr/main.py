"""FastAPI application factory and app instance.

``create_app`` wires configuration, the database engine/session factory, and
routes together. A module-level ``app`` is provided for ASGI servers
(``uvicorn collapsarr.main:app``) and for ``python -m collapsarr``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.orm import Session

from . import __version__
from .arr.routes import router as arr_router
from .arr.service import get_instance, list_path_mappings
from .arr.webhooks import (
    OnFileReadyHook,
    WebhookValidationError,
    default_on_file_ready_hook,
    parse_webhook_payload,
    resolve_webhook_file,
)
from .auth import api_key_middleware
from .config import Settings, get_settings
from .database import (
    create_engine_from_settings,
    create_session_factory,
    get_session,
    init_db,
)
from .jobs.queue import JobQueue
from .jobs.routes import router as jobs_router
from .jobs.scheduler import JobScheduler
from .media.routes import router as wanted_router
from .notify.routes import router as notifiers_router
from .settings.routes import router as settings_router


def create_app(
    settings: Settings | None = None,
    on_file_ready: OnFileReadyHook | None = None,
    *,
    enable_scheduler: bool = False,
) -> FastAPI:
    """Build and return a configured :class:`FastAPI` application.

    Passing ``settings`` overrides the cached process configuration, which is
    how tests inject an isolated database. ``on_file_ready`` overrides the
    "file ready" hook invoked by the arr webhook endpoint (see
    :mod:`collapsarr.arr.webhooks`); it defaults to a log-only stub.

    ``enable_scheduler`` (opt-in; the production ``app`` below sets it) wires
    the real Job Queue & Scheduler (COL-22): the webhook's "file ready" hook
    becomes :meth:`~collapsarr.jobs.scheduler.JobScheduler.on_file_ready`
    (enqueuing a real downmix job) and a background thread runs a periodic
    full-library scan. It is off by default so tests get the lightweight stub
    hook and no background thread unless they ask for it. An explicit
    ``on_file_ready`` always wins, so a test can inject its own hook regardless.
    The live :class:`~collapsarr.jobs.scheduler.JobScheduler` is exposed on
    ``app.state.job_scheduler`` (and its queue on ``app.state.job_queue``).
    """
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine_from_settings(resolved_settings)
        app.state.engine = engine
        session_factory = create_session_factory(engine)
        app.state.session_factory = session_factory
        init_db(engine)

        scheduler: JobScheduler | None = None
        if on_file_ready is None and enable_scheduler:
            queue = JobQueue.from_settings(resolved_settings)
            scheduler = JobScheduler(queue, session_factory, resolved_settings)
            app.state.job_queue = queue
            app.state.job_scheduler = scheduler
            app.state.on_file_ready = scheduler.on_file_ready
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()
            engine.dispose()

    app = FastAPI(
        title="Collapsarr",
        summary="Adds downmixed audio tracks to media missing them, via FFmpeg.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.on_file_ready = on_file_ready or default_on_file_ready_hook

    # Enforce the auto-generated API key on every /api route (COL-26).
    app.middleware("http")(api_key_middleware)

    # Instance config & path-mapping CRUD endpoints (COL-27), under /api.
    app.include_router(arr_router)

    # Global settings GET/PUT and the wanted-list GET (COL-28), under /api.
    app.include_router(settings_router)
    app.include_router(wanted_router)

    # Job history GET + on-demand scan/trigger POSTs (COL-29), under /api.
    app.include_router(jobs_router)

    # Notifier config GET/PUT (COL-36), under /api.
    app.include_router(notifiers_router)

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        """Liveness probe. Returns 200 with the running app version."""
        return {"status": "ok", "version": __version__}

    @app.post("/api/webhook/arr/{instance_id}", tags=["webhooks"])
    def arr_webhook(
        instance_id: int,
        payload: dict[str, Any],
        request: Request,
        session: Session = Depends(get_session),
    ) -> dict[str, str]:
        """Receive a Sonarr/Radarr "on import"/"on upgrade" webhook.

        ``instance_id`` names the configured :class:`~collapsarr.arr.models.ArrInstance`
        the webhook came from -- Sonarr/Radarr's own payload carries no
        instance identifier, so the sending instance must be configured to
        POST to this instance-specific URL. The affected file's path is
        resolved via that instance's path mappings and handed to the
        pluggable "file ready" hook (see :mod:`collapsarr.arr.webhooks`).

        Unknown event types (anything other than an import/upgrade
        ``Download`` event, e.g. Sonarr/Radarr's "Test" button) are
        acknowledged with 200 but otherwise ignored. Malformed payloads
        (missing ``eventType``, or a ``Download`` event missing the file data
        needed to resolve a path) are rejected with 422.
        """
        instance = get_instance(session, instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail=f"No arr instance with id={instance_id}")

        try:
            raw_file = parse_webhook_payload(instance.type, payload)
        except WebhookValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if raw_file is not None:
            mappings = list_path_mappings(session, instance.id)
            resolved = resolve_webhook_file(instance, raw_file, mappings)
            hook: OnFileReadyHook = request.app.state.on_file_ready
            hook(resolved)

        return {"status": "ok"}

    return app


app = create_app(enable_scheduler=True)
