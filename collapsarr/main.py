"""FastAPI application factory and app instance.

``create_app`` wires configuration, the database engine/session factory, and
routes together. A module-level ``app`` is provided for ASGI servers
(``uvicorn collapsarr.main:app``) and for ``python -m collapsarr``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.orm import Session

from . import __version__
from .arr.models import ArrInstance, InstanceType
from .arr.routes import router as arr_router
from .arr.service import get_instance, list_path_mappings
from .arr.webhooks import (
    OnFileReadyHook,
    ResolvedWebhookFile,
    WebhookValidationError,
    default_on_file_ready_hook,
    parse_webhook_payload,
    resolve_webhook_file,
)
from .auth import EnforceAuthMiddleware, SessionMiddleware, auth_router
from .backup.routes import router as backup_router
from .backup.scheduler import BackupScheduler
from .config import Settings, get_settings
from .database import (
    create_engine_from_settings,
    create_session_factory,
    get_session,
)
from .ffmpeg_download.routes import router as ffmpeg_download_router
from .frontend import mount_frontend
from .health import (
    DiskUsage,
    FfmpegCheckResult,
    HealthCheckScheduler,
    default_health_checks,
    list_failing_checks,
)
from .health.routes import router as health_checks_router
from .jobs.queue import JobQueue
from .jobs.rehydrate import rehydrate_pending_jobs
from .jobs.routes import router as jobs_router
from .jobs.scheduler import JobScheduler
from .library.routes import router as library_router
from .library.service import upsert_movie_node, upsert_series_episode_node
from .logging_setup import apply_log_level, configure_logging
from .media.routes import router as wanted_router
from .migrations import upgrade_to_head
from .notify.routes import router as notifiers_router
from .plex.routes import router as plex_router
from .plex.scheduler import PlexSyncScheduler
from .restore.engine import apply_pending_restore
from .restore.routes import router as restore_router
from .self_update.apply import ReexecFn, SubprocessRunner
from .self_update.health_gate import HealthCheckFn, resolve_awaiting_health
from .self_update.routes import router as self_update_router
from .settings.env_seed import seed_auth_from_env
from .settings.routes import router as settings_router
from .settings.service import get_global_settings, restore_auto_processing_pause
from .system.info import router as info_router
from .system.logs import router as logs_router
from .system.probe import DefaultSystemProbe, SystemProbe
from .system.tasks import router as tasks_router
from .update_check import UpdateCheckScheduler
from .update_check.routes import router as update_checks_router
from .url_base import UrlBaseMiddleware


def _sync_webhook_library_node(
    session: Session, instance: ArrInstance, resolved: ResolvedWebhookFile
) -> None:
    """Upsert the Library node(s) a webhook import event names (COL-102).

    Keeps the Library mirror (ADR-0002) current in real time -- the webhook
    counterpart to the periodic scan's :meth:`~collapsarr.jobs.scheduler.
    JobScheduler._sync_instance_library` full-catalog pass. Dispatches on the
    sending instance's type and upserts just the affected node's ancestry
    (Series > Season > Episode for Sonarr, a flat Movie for Radarr), so the
    node -- and its resolved **Tracked** value -- exists before the file-ready
    hook resolves it. ``has_file=True``: a ``Download`` import event fires only
    once the file is actually present. A payload missing the ids needed to key
    a node (e.g. a Sonarr event with no ``episodes`` array) is skipped rather
    than keyed on ``None``; the file-ready hook still runs, and the Tracked
    gate falls back to the global default when no node resolves.
    """
    if instance.type is InstanceType.SONARR:
        if (
            resolved.sonarr_series_id is None
            or resolved.sonarr_episode_id is None
            or resolved.season_number is None
        ):
            return
        upsert_series_episode_node(
            session,
            instance_id=instance.id,
            series_id=resolved.sonarr_series_id,
            series_title=resolved.media_title,
            season_number=resolved.season_number,
            episode_id=resolved.sonarr_episode_id,
            episode_number=resolved.episode_number if resolved.episode_number is not None else 0,
            episode_title=resolved.episode_title if resolved.episode_title is not None else "",
        )
    elif instance.type is InstanceType.RADARR:
        if resolved.radarr_movie_id is None:
            return
        upsert_movie_node(
            session,
            instance_id=instance.id,
            movie_id=resolved.radarr_movie_id,
            title=resolved.media_title,
        )


def create_app(
    settings: Settings | None = None,
    on_file_ready: OnFileReadyHook | None = None,
    *,
    enable_scheduler: bool = False,
    ffmpeg_checker: Callable[[], FfmpegCheckResult] | None = None,
    notify_transport: httpx.BaseTransport | None = None,
    arr_transport: httpx.BaseTransport | None = None,
    disk_usage: Callable[[str], DiskUsage] | None = None,
    update_check_transport: httpx.BaseTransport | None = None,
    plex_transport: httpx.BaseTransport | None = None,
    system_probe: SystemProbe | None = None,
    ffmpeg_download_transport: httpx.BaseTransport | None = None,
    self_update_transport: httpx.BaseTransport | None = None,
    self_update_subprocess_runner: SubprocessRunner | None = None,
    self_update_reexec_fn: ReexecFn | None = None,
    self_update_health_check_fn: HealthCheckFn | None = None,
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

    ``ffmpeg_checker`` overrides the FFmpeg presence probe registered on the
    Health Check Framework (COL-75; defaults to
    :func:`~collapsarr.health.check_ffmpeg`), letting tests simulate a
    present/missing FFmpeg without touching the real binary. ``notify_transport``
    is forwarded to both the Health Check Framework's transition notifications
    and the Update Check scheduler's edge-triggered "update available"
    notification (COL-89) -- one shared notifier config, so tests inject a
    single ``httpx.MockTransport`` to capture either; production leaves it
    ``None``. ``arr_transport``
    (COL-78) is forwarded to every Arr-instance connectivity probe the
    per-instance unreachable check makes, letting tests simulate
    reachable/unreachable instances without a real network call; production
    leaves it ``None`` for a real connectivity check against each configured
    instance. ``disk_usage`` (COL-79) overrides the disk-space check's
    :func:`shutil.disk_usage` probe, letting tests simulate an arbitrary
    free-space percentage without depending on the real filesystem's current
    usage; production leaves it ``None`` for a real reading against
    ``settings.data_dir``. ``update_check_transport`` (COL-86) is forwarded to
    the Update Check scheduler's GitHub Releases fetch (tests inject an
    ``httpx.MockTransport``; production leaves it ``None`` for a real network
    call). ``plex_transport`` (COL-210) is forwarded to the Plex Sync
    scheduler's Plex Media Server calls (tests inject an ``httpx.MockTransport``;
    production leaves it ``None`` for a real network call). ``system_probe``
    (COL-123) overrides the About panel's Python
    version / OS platform / FFmpeg version probe (see
    :class:`~collapsarr.system.probe.SystemProbe`); production leaves it
    ``None`` for the real, ``platform``/subprocess-backed
    :class:`~collapsarr.system.probe.DefaultSystemProbe`. ``ffmpeg_download_transport``
    (COL-222) is forwarded to ``POST /api/system/ffmpeg/download``'s
    download-and-verify call (see
    :func:`collapsarr.ffmpeg_download.service.download_and_install_ffmpeg`),
    letting tests inject an ``httpx.MockTransport`` instead of a real network
    call; production leaves it ``None``. Unlike the transports above, this one
    is not consumed by any background scheduler -- the download only ever
    runs synchronously inside that one request handler -- so it is stashed
    directly on ``app.state.ffmpeg_download_transport`` for the route to read,
    rather than threaded into a scheduler constructor. ``self_update_transport``/
    ``self_update_subprocess_runner``/``self_update_reexec_fn`` (COL-232) are
    the same idea for ``POST /api/system/self-update/apply``'s pipx apply
    flow (:func:`collapsarr.self_update.apply.apply_pipx_update`): the
    transport stands in for a real network call, and the latter two stand in
    for a real ``pipx upgrade`` subprocess spawn / a real :func:`os.execv`
    re-exec, letting tests exercise the whole apply flow with fakes/spies.
    All three are stashed directly on ``app.state`` (same as
    ``ffmpeg_download_transport``) and default to ``None``, in which case
    the route lets :func:`~collapsarr.self_update.apply.apply_pipx_update`'s
    own production defaults apply. ``self_update_health_check_fn`` (COL-234)
    is the equivalent seam for the post-re-exec health-check gate
    (:func:`collapsarr.self_update.health_gate.resolve_awaiting_health`,
    wired into this lifespan below): tests inject a fake that reports
    healthy/unhealthy on demand; production leaves it ``None``, in which case
    the lifespan wires a real one that reruns the Health Check Framework tick
    just above and reads back its persisted state -- see
    :mod:`collapsarr.self_update.health_gate`'s module docstring for why that,
    rather than a real HTTP call to ``/health``, is what "healthy" means here.
    Unlike the other two self-update seams, this one is consumed once, here in
    the lifespan (the gate runs at most once per process boot), not stashed on
    ``app.state`` for a route to read later.
    """
    resolved_settings = settings or get_settings()

    # Logging infrastructure (COL-128): a stdout + rotating-file handler pair
    # on the `collapsarr` logger, with secret redaction (ADR 0006). Configured
    # here -- before the lifespan below runs -- so startup-time logging (the
    # restore swap, Alembic migrations, scheduler bring-up) is captured too,
    # not just requests served after boot. Idempotent, so each call (once per
    # `create_app()`; tests build a fresh app per test with its own tmp_path
    # Settings) replaces rather than accumulates handlers.
    configure_logging(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Process start stamp (COL-123): taken first, before any startup work
        # below (restore swap, migrations, engine/scheduler setup) -- so
        # `uptime_seconds` (GET /api/system/info) measures from genuine
        # lifespan/process start rather than undercounting by however long
        # that startup sequence takes. A `time.monotonic()` stamp rather than
        # wall-clock time, so a system clock adjustment (NTP sync, DST) can't
        # produce a negative or jumping uptime later.
        app.state.process_start_monotonic = time.monotonic()

        # Boot-time staged swap (COL-70): before the engine connects and before
        # the schema upgrade below, apply a pending database restore if one is
        # marked -- take a safety backup of the current DB, swap the staged file
        # into place, and clear the marker. A missing/invalid staged file (or a
        # non-file database) aborts the swap and boots normally; no marker is a
        # clean no-op. This is the one window a raw file swap is safe: nothing has
        # opened the database yet, and the swapped-in (possibly older) DB is then
        # forward-migrated by upgrade_to_head.
        apply_pending_restore(resolved_settings)

        # Bring the schema up to head via Alembic before serving the first
        # request (COL-58). Alembic is the single source of truth for schema:
        # on a fresh install this runs the full migration chain from base; on an
        # already-current install it is a no-op. Any migration error propagates
        # out of the lifespan, so startup aborts (non-zero exit) rather than
        # serve against a half-migrated schema.
        upgrade_to_head(resolved_settings)

        engine = create_engine_from_settings(resolved_settings)
        app.state.engine = engine
        session_factory = create_session_factory(engine)
        app.state.session_factory = session_factory

        # One-shot Auto-Processing Pause restore (COL-233): consume whatever
        # the self-update apply flow stashed into `GlobalSettings.
        # auto_processing_pause_restore_value` (collapsarr.self_update.apply,
        # "Cancel & Restart Now"/"Wait & Restart") before force-pausing
        # processing for the duration of an apply -- write it back onto
        # `auto_processing_paused` and clear the restore column to `None`.
        # Run early, right after the database is available and before the Job
        # Queue below is even constructed, so a stashed pause never has a
        # window where a fresh worker could claim a Job against the *forced*
        # (not yet restored) pause state. A no-op on every ordinary boot (no
        # self-update has ever run, or the previous boot already consumed
        # it) -- see `collapsarr.settings.service.restore_auto_processing_pause`.
        with session_factory() as restore_pause_session:
            restore_auto_processing_pause(restore_pause_session)

        # Runtime log-level control (COL-130): a persisted `GlobalSettings.
        # log_level` override, applied now that the database is available --
        # `configure_logging` above already set the `collapsarr` logger to
        # the env-sourced `COLLAPSARR_LOG_LEVEL` floor before this lifespan
        # even started (no DB access that early). Left `None` (the default,
        # and every fresh install's starting state) is a no-op: that env
        # floor stands. A previously-set level survives a restart because
        # this re-applies it every boot, not just the first time it's set.
        with session_factory() as log_level_session:
            persisted_log_level = get_global_settings(log_level_session).log_level
        if persisted_log_level is not None:
            apply_log_level(persisted_log_level)

        # Environment-seeded UI credential for headless deploys (COL-53): if
        # COLLAPSARR_AUTH_USERNAME/PASSWORD are set and no credential exists
        # yet, persist one (hashed) now, before the first request, so the
        # instance comes up already past the /setup gate. No-op on every
        # later boot once a credential exists, even if the variables are
        # still set (see collapsarr.settings.env_seed).
        with session_factory() as seed_session:
            seed_auth_from_env(seed_session, resolved_settings)

        scheduler: JobScheduler | None = None
        job_queue: JobQueue | None = None
        if on_file_ready is None and enable_scheduler:
            job_queue = JobQueue.from_settings(resolved_settings)
            # Restart-durable rehydration (COL-166): reconstruct every still-
            # PENDING JobHistory row (left behind by whatever process was
            # running before this one) as a live Job, in persisted priority
            # order, *before* the worker pool below starts claiming work --
            # otherwise a PENDING row survives on disk forever with nothing
            # left to run it. Reuses this app's own session_factory rather
            # than from_settings()'s private one (see its docstring); same
            # on-disk SQLite database either way.
            with session_factory() as rehydrate_session:
                rehydrate_pending_jobs(rehydrate_session, job_queue)
            job_queue.start()  # spin up the persistent worker pool (COL-164)
            scheduler = JobScheduler(job_queue, session_factory, resolved_settings)
            app.state.job_queue = job_queue
            app.state.job_scheduler = scheduler
            app.state.on_file_ready = scheduler.on_file_ready
            scheduler.start()

        # Scheduled automatic database backups (COL-67): a separate daemon-thread
        # scheduler that takes a `scheduled` backup whenever the newest one on
        # disk is older than `backup_interval_days`. Gated on `enable_scheduler`
        # alone (independent of the webhook hook above), and cleanly stopped in
        # the `finally` below. No-ops for a non-file-based / `:memory:` database.
        backup_scheduler: BackupScheduler | None = None
        if enable_scheduler:
            backup_scheduler = BackupScheduler(resolved_settings, session_factory)
            app.state.backup_scheduler = backup_scheduler
            backup_scheduler.start()

        # Health Check Framework (COL-75): a pluggable set of registered checks
        # run by a dedicated daemon-thread scheduler on a fixed 5-minute cadence.
        # Replaces the old one-shot startup FFmpeg check + bespoke notifier -- the
        # FFmpeg presence probe is now one registered check whose result is diffed
        # against persisted per-check state, so a notification fires only on a
        # pass<->fail transition (never repeatedly for an unchanged still-failing
        # check) and /health is populated from that state. The first tick always
        # runs synchronously here, before `yield`, so /health is accurate the
        # instant the app comes up and any startup pass->fail notification fires --
        # exactly the deterministic-at-startup guarantee the retired one-shot
        # startup check gave. With the scheduler enabled the background thread then
        # takes over the recurring 5-minute cadence for subsequent ticks
        # (run_immediately=False, so it does not redundantly re-tick right away);
        # with it disabled (tests, one-shot use) the single synchronous tick above
        # is all that runs.
        checks = default_health_checks(
            ffmpeg_checker, arr_transport=arr_transport, disk_usage=disk_usage
        )
        health_scheduler = HealthCheckScheduler(
            resolved_settings, session_factory, checks, transport=notify_transport
        )
        app.state.health_scheduler = health_scheduler
        health_scheduler.run_once()

        # Self-update health-check gate + pinned-reinstall rollback (COL-234):
        # a no-op on every ordinary boot -- only the one boot immediately
        # following COL-232's pipx apply-flow re-exec has
        # `phase == PHASE_AWAITING_HEALTH` for this to act on. Placed right
        # after the Health Check Framework's own first tick above so its
        # default health_check_fn (rerun that tick, then read back
        # list_failing_checks) reflects this exact boot, not a stale one.
        # Deliberately before `health_scheduler.start()` below: a rollback
        # here re-execs this process outright (see
        # collapsarr.self_update.health_gate's module docstring), so nothing
        # after it in a real boot ever runs anyway.
        def _self_update_health_check_fn() -> bool:
            health_scheduler.run_once()
            with session_factory() as probe_session:
                return not list_failing_checks(probe_session)

        with session_factory() as self_update_session:
            resolve_awaiting_health(
                self_update_session,
                health_check_fn=self_update_health_check_fn or _self_update_health_check_fn,
                subprocess_runner=self_update_subprocess_runner,
                reexec_fn=self_update_reexec_fn,
            )

        if enable_scheduler:
            health_scheduler.start(run_immediately=False)

        # Update Check scheduler (COL-86, COL-89): a dedicated daemon-thread
        # scheduler, structurally identical to the Health Check Framework's
        # above, that fetches the latest GitHub Release on a fixed 24-hour
        # cadence and persists it to the singleton `update_check_state` row.
        # Same run-the-first-tick-synchronously-then-hand-off-to-the-thread
        # shape, so the cached release data is accurate the instant the app
        # comes up. GET/POST /api/system/updates{,/recheck,/dismiss,
        # /undismiss} (COL-87/COL-89, update_checks_router below) expose this
        # state. A tick whose fetched `latest_tag` differs from the
        # previously-stored one fires an edge-triggered notification over the
        # same `notify_transport` the health framework's transitions use
        # (COL-89) -- one shared notifier config, one shared mock transport in
        # tests.
        update_check_scheduler = UpdateCheckScheduler(
            resolved_settings,
            session_factory,
            transport=update_check_transport,
            notify_transport=notify_transport,
        )
        app.state.update_check_scheduler = update_check_scheduler
        update_check_scheduler.run_once()
        if enable_scheduler:
            update_check_scheduler.start(run_immediately=False)

        # Plex Sync scheduler (COL-210): a dedicated daemon-thread scheduler,
        # structurally a sibling of the four above, that rebuilds the Plex
        # Library Item mapping table (path -> ratingKey) on a fixed weekly
        # cadence. Wired on app.state unconditionally so the manual
        # `POST /api/plex/sync` "Run now" and the on-save `request_sync` hook
        # (PUT /api/plex/connection) always have a target -- but, unlike the
        # health/update schedulers, its first tick is NOT run synchronously at
        # startup (a full Plex walk is heavy and its output only feeds the
        # resolution *fallback*, so there's no "accurate the instant we boot"
        # requirement). The background loop -- which takes an immediate first
        # tick itself -- runs only when enable_scheduler is set, matching the
        # job/backup schedulers. Cleanly stopped in the `finally` below.
        plex_sync_scheduler = PlexSyncScheduler(
            resolved_settings, session_factory, transport=plex_transport
        )
        app.state.plex_sync_scheduler = plex_sync_scheduler
        if enable_scheduler:
            plex_sync_scheduler.start()

        # About-panel system info (COL-123): the injectable Python-version/
        # OS-platform/FFmpeg-version probe (collapsarr.system.probe.SystemProbe)
        # backing GET /api/system/info. FFmpeg version alone requires a
        # subprocess call (`ffmpeg -version`), so -- unlike Python version/OS
        # platform, which are cheap stdlib reads taken fresh on every request --
        # it is probed exactly once here, at startup, and cached on
        # `app.state.ffmpeg_version`: it cannot change without a process
        # restart. `app.state.disk_usage` re-exposes this factory's own
        # `disk_usage` override so the info endpoint reads free/total bytes off
        # the identical injected probe the disk-space health check uses in
        # tests, rather than a second, real `shutil.disk_usage` call.
        resolved_system_probe = system_probe or DefaultSystemProbe()
        app.state.system_probe = resolved_system_probe
        app.state.disk_usage = disk_usage
        app.state.ffmpeg_version = resolved_system_probe.ffmpeg_version()

        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()
            # Stop the queue's worker pool after the scheduler (no new work is
            # enqueued once the scan loop is stopped); any in-flight job runs
            # to completion first (COL-164).
            if job_queue is not None:
                job_queue.shutdown()
            if backup_scheduler is not None:
                backup_scheduler.stop()
            health_scheduler.stop()
            update_check_scheduler.stop()
            plex_sync_scheduler.stop()
            engine.dispose()

    app = FastAPI(
        title="Collapsarr",
        summary="Adds downmixed audio tracks to media missing them, via FFmpeg.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.on_file_ready = on_file_ready or default_on_file_ready_hook
    app.state.ffmpeg_download_transport = ffmpeg_download_transport
    app.state.self_update_transport = self_update_transport
    app.state.self_update_subprocess_runner = self_update_subprocess_runner
    app.state.self_update_reexec_fn = self_update_reexec_fn

    # Exposed for GET /api/system/tasks (COL-122) to tell whether a Scheduled
    # Task's computed next-run time is actually meaningful: the health/update
    # check schedulers are wired unconditionally below regardless of this
    # flag (their first tick always runs synchronously so /health is
    # accurate at startup), so their presence on app.state can't answer
    # "is the periodic background loop running" the way it can for the
    # job/backup schedulers, which are only wired when this flag is set.
    app.state.enable_scheduler = enable_scheduler

    # Auth (COL-50): first-run setup + Forms login gate the whole UI behind a
    # signed-cookie session; /api still accepts the API key. The enforcement
    # middleware is added first (inner) and the session middleware last (outer)
    # so the session is decoded onto the request scope *before* enforcement
    # reads it. Both supersede the old opt-in api_key_middleware (COL-26).
    #
    # EnforceAuthMiddleware is a raw-ASGI middleware class (COL-199), not the
    # old app.middleware("http")(...) decorator form -- Starlette wraps the
    # decorator form in a BaseHTTPMiddleware that re-buffers the response body
    # through its own anyio streams, a hazard for the large streamed
    # FileResponse the backup-download route returns. The raw-ASGI class hands
    # the ASGI send straight through on the happy path (see its docstring).
    app.add_middleware(EnforceAuthMiddleware)
    app.add_middleware(SessionMiddleware)

    # URL base (COL-116): strips a configured COLLAPSARR_URL_BASE prefix from
    # the incoming path and sets ASGI root_path, so a reverse proxy can pass
    # the full external path straight through with no rewrite rule (ADR-0004).
    # Registered *last* here, which -- per Starlette's add_middleware, which
    # inserts each new middleware at the front of the stack -- makes it the
    # *outermost* layer, running before both the session and enforcement
    # middleware above see the request (they need the already-stripped path/
    # already-set root_path). A no-op pass-through when url_base is unset.
    app.add_middleware(UrlBaseMiddleware, url_base=resolved_settings.url_base)

    # Forms auth endpoints: /api/auth/{status,setup,login,logout} (COL-50).
    app.include_router(auth_router)

    # Instance config & path-mapping CRUD endpoints (COL-27), under /api.
    app.include_router(arr_router)

    # Global settings GET/PUT and the wanted-list GET (COL-28), under /api.
    app.include_router(settings_router)
    app.include_router(wanted_router)

    # Job history GET + on-demand scan/trigger POSTs (COL-29), under /api.
    app.include_router(jobs_router)

    # Read-only Library tree GET /api/library/instances/{id}/tree (COL-98),
    # under /api. The per-instance Sonarr Series > Season > Episode mirror the
    # periodic scan keeps in sync, each node carrying its resolved Tracked value.
    app.include_router(library_router)

    # Notifier config GET/PUT (COL-36), under /api.
    app.include_router(notifiers_router)

    # Plex connection GET/PUT (COL-209), under /api. The Plex token never
    # leaves this router's PUT request body -- every response omits it.
    app.include_router(plex_router)

    # Database backup list/create (COL-63), under /api/system.
    app.include_router(backup_router)

    # Restore from a listed backup (COL-71), under /api/system. Stages the
    # database + arms the marker consumed by the boot-time swap engine
    # (COL-70); registered after backup_router but distinguished by the
    # POST /backup/restore/{id} path and method, so it never shadows the
    # GET .../download or DELETE .../{id} routes above.
    app.include_router(restore_router)

    # Full per-check detail GET /api/system/health-checks (COL-76): every
    # registered check's current state (passing or failing), driving the
    # System > Health list page. Distinct from the unauthenticated /health
    # probe below, which stays minimal and failing-only for the app-wide banner.
    app.include_router(health_checks_router)

    # Update Check state GET/POST /api/system/updates{,/recheck} (COL-87):
    # exposes the singleton state COL-86's scheduler keeps warm, driving the
    # System > Updates page and the app-wide "update available" indicator.
    app.include_router(update_checks_router)

    # Scheduled Task registry GET /api/system/tasks (COL-122): aggregates the
    # four background schedulers above into one list with cadence/next-run,
    # driving the System > Tasks page. Reads each scheduler's existing state
    # directly rather than a shared abstraction (ADR-0005) -- registered last
    # among the /api/system routers since it depends on state every one of
    # them already establishes.
    app.include_router(tasks_router)

    # About-panel system-info GET /api/system/info (COL-123): environment/
    # runtime facts (versions, OS, DB engine/schema revision, data paths,
    # uptime, timezone, disk usage) driving the new Status page. Kept in its
    # own module (collapsarr/system/info.py), separate from
    # collapsarr/system/tasks.py, purely so the two tickets can land commits
    # on the same Epic branch without touching the same file -- not a deeper
    # architectural split; both are thin /api/system aggregation views over
    # existing state (docs/adr/0005-system-tasks-endpoint-not-shared-scheduler.md).
    app.include_router(info_router)

    # Opt-in FFmpeg auto-download trigger POST /api/system/ffmpeg/download
    # (COL-222): downloads the pinned, checksum-verified build for this
    # platform (COL-217), extracts it into <data_dir>/ffmpeg/, and persists
    # the resolved path onto GlobalSettings.ffmpeg_path (COL-218) -- gated to
    # install_method != "docker" (COL-215). Surfaced from the ffmpeg_missing
    # health-check banner's opt-in "Download FFmpeg" action; never triggered
    # automatically (ADR 0001/0002).
    app.include_router(ffmpeg_download_router)

    # Self-update status/liveness GET /api/system/self-update/status (COL-230,
    # Epic COL-224): exposes the singleton self-update state's current phase
    # and in-progress guard -- foundational for the apply flows (COL-232+)
    # and the frontend's future polling screen; this ticket only wires up the
    # read side, there is no start endpoint yet.
    app.include_router(self_update_router)

    # Tail-read GET /api/system/logs (COL-131): the most recent lines of the
    # current rotating log file COL-128's configure_logging() writes to
    # (collapsarr/logging_setup.py), with an offset to page further back and a
    # minimum-severity level filter, driving the new System > Logs page. Reads
    # the file fresh per request (see collapsarr/system/logs.py's module
    # docstring for why) rather than depending on any app.state the other
    # /api/system routers above establish, so registration order relative to
    # them doesn't matter -- kept last simply to group with its siblings.
    app.include_router(logs_router)

    @app.get("/health", tags=["system"])
    def health(session: Session = Depends(get_session)) -> dict[str, object]:
        """Liveness probe. Returns 200 with the running app version and any
        currently-failing health checks (COL-75).

        ``status`` is ``"ok"`` unless a health check is currently failing -- e.g.
        FFmpeg availability -- in which case it is ``"degraded"`` and
        ``warnings`` carries one ``{"code", "message", "severity"}`` entry per
        failing check, read from the framework's persisted per-check state
        (populated by :class:`~collapsarr.health.HealthCheckScheduler`).
        ``severity`` (COL-76) is ``"warning"`` or ``"error"``, letting the
        frontend banner style multiple simultaneous warnings differently from
        errors; it was added alongside ``code``/``message`` as a
        backward-compatible field, not a rename (see ``CONTEXT.md``'s Check
        Code entry on why ``code`` values themselves are frozen). The app still
        starts and serves requests either way (so the UI and API stay usable),
        but a "degraded" status is the health-page signal that something (e.g. a
        missing FFmpeg blocking downmix jobs) needs attention. For the full
        detail of *every* registered check (passing or failing), see the
        authenticated ``GET /api/system/health-checks`` (COL-76).
        """
        warnings: list[dict[str, str]] = [
            {"code": state.code, "message": state.message, "severity": state.severity}
            for state in list_failing_checks(session)
        ]
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
            # Mirror the imported node into the Library (COL-102) *before* the
            # file-ready hook runs, so the file's resolved Tracked value is
            # already resolvable when the hook decides whether to auto-enqueue.
            _sync_webhook_library_node(session, instance, resolved)
            hook: OnFileReadyHook = request.app.state.on_file_ready
            hook(resolved)

        return {"status": "ok"}

    # Serve the bundled single-page frontend (COL-40). Registered last so the
    # catch-all SPA mount at "/" does not shadow the API/health routes above.
    # No-op in a source checkout without a built frontend (API stays usable).
    mount_frontend(app, url_base=resolved_settings.url_base)

    return app


app = create_app(enable_scheduler=True)
