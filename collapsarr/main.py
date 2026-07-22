"""FastAPI application factory and app instance.

``create_app`` wires configuration, the database engine/session factory, and
routes together. A module-level ``app`` is provided for ASGI servers
(``uvicorn collapsarr.main:app``) and for ``python -m collapsarr``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
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
from .frontend import mount_frontend
from .health import FfmpegCheckResult, check_ffmpeg, notify_ffmpeg_missing
from .jobs.queue import JobQueue
from .jobs.routes import router as jobs_router
from .jobs.scheduler import JobScheduler
from .media.routes import router as wanted_router
from .notify.routes import router as notifiers_router
from .settings.routes import router as settings_router

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    on_file_ready: OnFileReadyHook | None = None,
    *,
    enable_scheduler: bool = False,
    ffmpeg_checker: Callable[[], FfmpegCheckResult] | None = None,
    notify_transport: httpx.BaseTransport | None = None,
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

    ``ffmpeg_checker`` overrides the FFmpeg startup health check (COL-38;
    defaults to :func:`~collapsarr.health.check_ffmpeg`), letting tests
    simulate a present/missing FFmpeg without touching the real binary.
    ``notify_transport`` is forwarded to
    :func:`~collapsarr.health.notify_ffmpeg_missing` (tests inject an
    ``httpx.MockTransport``; production leaves it ``None``).
    """
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine_from_settings(resolved_settings)
        app.state.engine = engine
        session_factory = create_session_factory(engine)
        app.state.session_factory = session_factory
        init_db(engine)

        # FFmpeg presence check (COL-38): run once at startup rather than let
        # a missing binary surface as a cryptic mid-job failure. The result is
        # exposed on /health as a "degraded" warning; a missing FFmpeg also
        # fans a notification out to every enabled notifier, if configured.
        checker = ffmpeg_checker or check_ffmpeg
        ffmpeg_check = checker()
        app.state.ffmpeg_check = ffmpeg_check
        if not ffmpeg_check.available:
            logger.error("Startup health check failed: %s", ffmpeg_check.detail)
            with session_factory() as health_session:
                notify_ffmpeg_missing(health_session, ffmpeg_check, transport=notify_transport)

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
    def health(request: Request) -> dict[str, object]:
        """Liveness probe. Returns 200 with the running app version and any
        startup health warnings (COL-38).

        ``status`` is ``"ok"`` unless a startup check failed -- currently just
        FFmpeg availability -- in which case it is ``"degraded"`` and
        ``warnings`` carries one entry per failed check. The app still starts
        and serves requests either way (so the UI and API stay usable), but a
        "degraded" status is the health-page signal that downmix jobs will
        fail until the underlying issue (e.g. installing FFmpeg) is fixed.
        """
        ffmpeg_check: FfmpegCheckResult = request.app.state.ffmpeg_check
        warnings: list[dict[str, str]] = []
        if not ffmpeg_check.available:
            warnings.append({"code": "ffmpeg_missing", "message": ffmpeg_check.detail})
        return {
            "status": "ok" if not warnings else "degraded",
            "version": __version__,
            "warnings": warnings,
        }

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

    # Serve the bundled single-page frontend (COL-40). Registered last so the
    # catch-all SPA mount at "/" does not shadow the API/health routes above.
    # No-op in a source checkout without a built frontend (API stays usable).
    mount_frontend(app)

    return app


app = create_app(enable_scheduler=True)
