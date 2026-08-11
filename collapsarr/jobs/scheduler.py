"""Wire the webhook and a periodic library scan into the job queue (COL-22).

Two automatic triggers feed the same de-duplicating enqueue path:

- **Real-time (webhook):** :meth:`JobScheduler.on_file_ready` is the "file
  ready" hook the arr webhook receiver (COL-14) calls once per imported/upgraded
  file. It replaces the log-only stub
  (:func:`collapsarr.arr.webhooks.default_on_file_ready_hook`); the app wires it
  in via ``create_app(enable_scheduler=True)`` (see :mod:`collapsarr.main`).
- **Periodic (scan):** a background thread runs a full-library scan every
  ``settings.scan_interval_hours`` (:meth:`scan_once`), pulling each configured
  instance's monitored file list (COL-12) and enqueuing every file that has a
  qualifying missing downmix target (COL-16).

Both funnel through :meth:`enqueue_file`, which probes the file
(:func:`~collapsarr.downmix.probe.probe_audio_streams`, COL-15), records the
probed streams onto tracked media
(:func:`~collapsarr.media.service.upsert_tracked_media`, COL-25/COL-95 --
the Wanted view's data source), asks
:func:`~collapsarr.downmix.targets.detect_qualifying_targets` whether any target
actually qualifies, and enqueues a real :class:`~collapsarr.jobs.queue.Job` only
when one does -- a file with nothing to do is never enqueued (but is still
tracked, correctly, as fully processed).

COL-23 adds two manual, on-demand entry points for a future API/UI ("Scan now"
and "trigger this file") to call, on top of the automatic ones above:

- :meth:`scan_now` -- an intention-revealing alias for :meth:`scan_once`. The
  scan logic already runs synchronously and doesn't wait on the background
  loop, so "run it immediately" needs no new logic, only a name a manual
  trigger can call without reaching for the periodic-scan method directly.
- :meth:`trigger_file` -- like :meth:`enqueue_file`, but lets the caller pass
  ``extra_languages`` to reach languages the scheduler's
  ``language_allow_list`` would otherwise exclude, for a one-off manual
  override (e.g. a user forcing a downmix for a language they normally don't
  want auto-processed) without mutating the process-wide
  ``self._downmix_settings`` used by every other trigger.

COL-155 adds a third manual, on-demand entry point, for the other half of the
Preferred Default Audio feature (COL-151/COL-152/COL-153):

- :meth:`trigger_set_default_audio` -- probes the file, resolves which
  existing audio stream should carry the Default Audio Track disposition
  via :func:`~collapsarr.downmix.default_audio.resolve_default_audio_stream`
  against the persisted preference (:func:`~collapsarr.settings.service.
  as_default_audio_preference`), and enqueues a ``SET_DEFAULT_AUDIO`` job
  (:meth:`~collapsarr.jobs.queue.JobQueue.enqueue_default_audio`) only when
  the resolved winner differs from what the file already has -- otherwise
  returns ``None`` ("skipped"), mirroring :meth:`trigger_file`'s skip
  semantics. Like :meth:`trigger_file`, it bypasses the **Tracked** gate
  (COL-102): a manual trigger is an explicit user action. It shares
  :meth:`enqueue_file`'s de-duplication (below) with every ``DOWNMIX``
  trigger, since both job kinds run through the same
  :class:`~collapsarr.jobs.queue.JobQueue` and are matched purely by file
  path -- an in-flight ``DOWNMIX`` job for a file blocks a
  ``SET_DEFAULT_AUDIO`` trigger for it, and vice versa.

De-duplication
--------------
Overlapping triggers (a webhook firing while a scan is mid-flight, or two scans
straddling a slow job) must not enqueue the same file twice. A file is
considered a duplicate -- and skipped -- when either:

- **already queued:** any job for that file path is currently ``PENDING`` or
  ``RUNNING`` in the in-memory queue (:meth:`~collapsarr.jobs.queue.JobQueue.list_jobs`);
  or
- **recently processed:** a persisted job-history row (COL-21) for that file
  path reached a terminal state (``SUCCEEDED``/``FAILED``) within the
  de-duplication window.

The window is ``GlobalSettings.recently_processed_window_minutes`` (COL-167;
:mod:`collapsarr.settings.models`), read **live** from the settings service on
every check -- not cached at construction, unlike ``scan_interval_hours``
(:attr:`_interval_seconds`), which the loop *does* cache since it only governs
the background thread's own sleep/wake cadence and has no "must react
instantly to a settings change" requirement. This field used to be silently
derived from ``scan_interval_hours`` (``timedelta(hours=settings.
scan_interval_hours)``, cached once in ``__init__``); COL-167 decouples the
two so the dedup cooldown can be tuned independently, and so a ``PUT
/api/settings`` change takes effect on the very next dedup check with no
restart or scheduler reconstruction -- ``concurrency_limit`` still needs a
restart (the worker pool's thread count is fixed at construction), but there
is no equivalent structural reason to require one here. ``0`` disables the
cooldown entirely: every check treats every file as eligible, i.e. a file is
never considered "recently processed".

The reasoning for having a cooldown at all: a successful downmix rewrites the
file, so the next scan's re-probe would already report "nothing to do" -- but
a *failed* run leaves the file unchanged and would otherwise be re-enqueued by
every subsequent trigger. The default (360 minutes / 6h, matching
``scan_interval_hours``'s own default) means a file is attempted at most once
per scan cycle by default, which both stops a webhook + scheduled scan from
double-enqueuing within a cycle and prevents a persistently-failing file from
being retried faster than once per cycle, while still allowing a periodic
retry after the window elapses. Checking persisted history (not just the
in-memory queue) also covers files processed in a *previous* process run:
after a restart the in-memory queue is empty, but a file downmixed minutes
before the restart is still correctly skipped.

The dedup check plus the enqueue are performed under a lock so the webhook
thread and the scan thread can't both pass the "not a duplicate" check for the
same file and each enqueue it.

Threads, not asyncio: this matches :mod:`collapsarr.jobs.queue`'s rationale --
the pipeline shells out to blocking ``ffprobe``/``ffmpeg`` subprocesses -- and
avoids pulling in an external scheduler dependency (there is none in
``pyproject.toml``). The loop is a plain sleep/wake ``threading`` loop: it wakes
on the scan-interval timeout to run a full scan, or early (:attr:`_wake`) to
break the wait on shutdown. It does not run jobs itself -- since COL-164 the
:class:`~collapsarr.jobs.queue.JobQueue`'s persistent worker pool runs each job
as soon as it is enqueued (whether by a scan or a webhook), so there is no
separate drain step to trigger.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.arr.catalog import (
    MalformedCatalogResponse,
    RadarrCatalog,
    SonarrCatalog,
    fetch_radarr_catalog,
    fetch_sonarr_catalog,
)
from collapsarr.arr.files import fetch_monitored_files
from collapsarr.arr.models import ArrInstance, InstanceType, resolve_path
from collapsarr.arr.service import list_instances, list_path_mappings
from collapsarr.arr.webhooks import ResolvedWebhookFile
from collapsarr.config import Settings
from collapsarr.downmix.default_audio import DefaultAudioPreference, resolve_default_audio_stream
from collapsarr.downmix.probe import AudioStreamInfo, FfprobeError, probe_audio_streams
from collapsarr.downmix.targets import DownmixSettings, detect_qualifying_targets
from collapsarr.jobs.history import list_job_history
from collapsarr.jobs.queue import Job, JobQueue, JobStatus
from collapsarr.library.service import (
    get_node_by_source_id,
    list_nodes,
    resolve_tracked,
    sync_library,
)
from collapsarr.media.service import upsert_tracked_media
from collapsarr.settings.service import as_default_audio_preference, get_global_settings

logger = logging.getLogger(__name__)

#: Signature of the probe seam: turn a file path into its audio streams. Matches
#: :func:`~collapsarr.downmix.probe.probe_audio_streams` (called positionally),
#: and is injectable so tests need neither ``ffprobe`` nor real media files.
ProbeFn = Callable[[Path], Sequence[AudioStreamInfo]]

#: Signature of the catalog-fetch seam: pull a Sonarr instance's full catalog.
#: Matches :func:`~collapsarr.arr.catalog.fetch_sonarr_catalog`, and is
#: injectable so the library-sync integration test needs no real Sonarr.
CatalogFetchFn = Callable[[ArrInstance], SonarrCatalog]

#: Signature of the Radarr catalog-fetch seam (COL-99): pull a Radarr
#: instance's full, flat movie catalog. Matches
#: :func:`~collapsarr.arr.catalog.fetch_radarr_catalog`, and is injectable so
#: the library-sync integration test needs no real Radarr.
RadarrCatalogFetchFn = Callable[[ArrInstance], RadarrCatalog]

#: A job is "in flight" -- and so a duplicate -- when in either of these states.
_ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)
#: A job counts as "recently processed" only once it has reached one of these.
_TERMINAL_STATUSES = (JobStatus.SUCCEEDED, JobStatus.FAILED)

_STOP_JOIN_TIMEOUT = 5.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobScheduler:
    """Enqueue downmix jobs from webhooks and a periodic scan, de-duplicating both.

    ``queue`` is the shared :class:`~collapsarr.jobs.queue.JobQueue` both
    triggers enqueue onto (its worker pool runs them, COL-164). ``session_factory``
    opens sessions for reading configured instances, path mappings, and job
    history. ``settings`` supplies ``scan_interval_hours``, the periodic scan
    loop's own cadence -- and, as of COL-167, *only* that; the "recently
    processed" dedup window is a separate, persisted
    ``GlobalSettings.recently_processed_window_minutes`` value read live on
    every dedup check (see the module docstring).

    ``downmix_settings`` is the target/language configuration every enqueued job
    is created with; it defaults to :class:`~collapsarr.downmix.targets.DownmixSettings`'s
    own defaults (Stereo only). There is no persisted, per-instance Settings
    model yet -- a single process-wide default mirrors how the rest of the
    downmix engine already takes a ``DownmixSettings`` argument, and a real
    settings store can be threaded through here later.

    ``probe``, ``catalog_fetch``, ``radarr_catalog_fetch`` and ``now`` are
    injectable seams for testing (a stub probe, stub Sonarr/Radarr full-catalog
    fetches, and a controllable clock); all default to the real
    implementations. ``catalog_fetch`` (COL-98) and ``radarr_catalog_fetch``
    (COL-99) are what the scan uses to mirror each Sonarr/Radarr instance's
    catalog into the Library on the same cadence -- see :meth:`scan_once`.
    """

    def __init__(
        self,
        queue: JobQueue,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        downmix_settings: DownmixSettings | None = None,
        probe: ProbeFn = probe_audio_streams,
        catalog_fetch: CatalogFetchFn = fetch_sonarr_catalog,
        radarr_catalog_fetch: RadarrCatalogFetchFn = fetch_radarr_catalog,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._queue = queue
        self._session_factory = session_factory
        self._settings = settings
        self._downmix_settings = downmix_settings or DownmixSettings()
        self._probe = probe
        self._catalog_fetch = catalog_fetch
        self._radarr_catalog_fetch = radarr_catalog_fetch
        self._now = now
        self._interval_seconds = settings.scan_interval_hours * 3600.0
        self._enqueue_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_scan_at: datetime | None = None
        self._not_tracked_logged: dict[Path, datetime] = {}
        self._bridge_missing_logged: dict[Path, datetime] = {}
        self._not_tracked_log_lock = threading.Lock()

    @property
    def last_scan_at(self) -> datetime | None:
        """UTC timestamp the most recent :meth:`scan_once` run started, or ``None`` (COL-122).

        ``None`` until the first scan (manual or periodic) actually runs --
        there is no persisted store for this, only an in-memory marker stamped
        at the top of :meth:`scan_once`, so it resets on every process
        restart same as the periodic loop's own ``time.monotonic()``-based
        "next scan" bookkeeping. Exists so ``GET /api/system/tasks``
        (:mod:`collapsarr.system.tasks`) can compute the Library Scan
        Scheduled Task's next-run time the same way the other three
        schedulers already expose theirs from their own existing state (see
        ``docs/adr/0005-system-tasks-endpoint-not-shared-scheduler.md``).
        """
        return self._last_scan_at

    # -- Enqueue path (shared by webhook + scan) ----------------------------

    def on_file_ready(self, file: ResolvedWebhookFile) -> None:
        """Webhook "file ready" hook: enqueue a real downmix job for the file.

        The path on ``file`` has already been translated through the instance's
        path mappings by :func:`~collapsarr.arr.webhooks.resolve_webhook_file`,
        so it is a host-local path ready to probe. Enqueuing a job (rather than
        running the pipeline inline) keeps the webhook response fast; the
        queue's worker pool picks it up as soon as a worker is free.

        Passes ``file``'s ``instance_id``/``sonarr_episode_id``/
        ``radarr_movie_id`` (COL-101) through to :meth:`enqueue_file` so the
        resulting tracked-media row carries the Library-node bridge from the
        moment a file first shows up via webhook.
        """
        job = self.enqueue_file(
            file.file_path,
            instance_id=file.instance_id,
            sonarr_episode_id=file.sonarr_episode_id,
            radarr_movie_id=file.radarr_movie_id,
        )
        if job is None:
            logger.info(
                "webhook: no job enqueued for %s (duplicate or nothing to do)",
                file.file_path,
            )
            return
        logger.info("webhook: enqueued job %s for %s", job.id, file.file_path)

    def enqueue_file(
        self,
        file_path: str | Path,
        *,
        session: Session | None = None,
        settings: DownmixSettings | None = None,
        instance_id: int | None = None,
        sonarr_episode_id: int | None = None,
        radarr_movie_id: int | None = None,
        respect_tracked: bool = True,
    ) -> Job | None:
        """Enqueue a downmix job for ``file_path`` unless it should be skipped.

        Returns the created :class:`~collapsarr.jobs.queue.Job`, or ``None`` when
        the file is a duplicate (already queued / recently processed), resolves
        to **Not Tracked** (``respect_tracked`` -- see below), has no qualifying
        downmix target, or cannot be probed. ``session`` (when given) is reused
        for the history-based dedup lookup and the tracked-media upsert below;
        otherwise a short-lived one is opened.

        ``respect_tracked`` (COL-102) gates the enqueue on the file's resolved
        **Tracked** value (``CONTEXT.md``): when ``True`` (the automatic paths --
        :meth:`on_file_ready`, :meth:`scan_once`) a file whose owning
        :class:`~collapsarr.library.models.LibraryNode` resolves to Not Tracked
        is *tracked* (its media row is still upserted, below) but never
        auto-enqueued -- Tracked gates automatic behavior only. :meth:`trigger_file`
        passes ``False`` so an explicit manual trigger still downmixes a
        Not-Tracked file. The gate is a no-op when there is no catalog identity
        to resolve (``instance_id`` / episode / movie id all absent, e.g. a
        bare-path manual trigger): such a file can't be bridged to a node, so it
        is treated as Tracked and proceeds -- exactly the pre-COL-102 behavior.

        ``settings`` overrides :attr:`_downmix_settings` for this call only --
        used by :meth:`trigger_file` (COL-23) to pass a per-call allow-list
        override without mutating the scheduler's own default. It defaults to
        ``self._downmix_settings``, which is what :meth:`on_file_ready` and
        :meth:`scan_once` implicitly use.

        ``instance_id``/``sonarr_episode_id``/``radarr_movie_id`` (COL-101) are
        the Arr instance's own object ids for ``file_path``, when the caller
        has them (:meth:`on_file_ready` and :meth:`scan_once` do;
        :meth:`trigger_file`'s manual-trigger-by-bare-path callers don't).
        Passed straight through to :meth:`_track_media` -- see
        :func:`~collapsarr.media.service.upsert_tracked_media` for how an
        id-less call is handled without clobbering a previously-established
        linkage.

        The cheap dedup check runs first so an already-handled file isn't probed
        needlessly. It is re-checked under :attr:`_enqueue_lock` immediately
        before enqueuing so two concurrent triggers can't both enqueue the same
        file.

        Once probed, :func:`~collapsarr.media.service.upsert_tracked_media` is
        called unconditionally -- before the qualifying-target check below --
        so every probed file's tracked-media row reflects its current status
        (COL-95): a file with a missing target is recorded ``MISSING`` (and so
        appears in the Wanted view even though nothing was enqueued for it
        yet, on the *next* qualifying probe -- see the early return above,
        which only skips *duplicates*), and a file that already has every
        enabled target is correctly recorded ``PROCESSED`` rather than left
        untracked, even though :meth:`enqueue_file` returns ``None`` for it
        either way.
        """
        path = Path(file_path)
        effective_settings = settings if settings is not None else self._downmix_settings

        if self._is_duplicate(path, session):
            return None

        try:
            streams = self._probe(path)
        except FfprobeError as exc:
            logger.warning("skipping %s: could not probe audio streams: %s", path, exc)
            return None

        self._track_media(
            path,
            streams,
            effective_settings,
            session,
            instance_id=instance_id,
            sonarr_episode_id=sonarr_episode_id,
            radarr_movie_id=radarr_movie_id,
        )

        # Tracked gate (COL-102): an automatic trigger never auto-enqueues a
        # Not-Tracked file. Placed *after* _track_media so the file is still
        # mirrored/tracked (it just isn't queued) and *before* the enqueue, so
        # nothing about an already-queued/running job or produced tracks is
        # touched -- Tracked only gates *future* automatic enqueueing.
        if respect_tracked and not self._resolve_tracked(
            session,
            instance_id=instance_id,
            sonarr_episode_id=sonarr_episode_id,
            radarr_movie_id=radarr_movie_id,
            path=path,
        ):
            if self._should_log_not_tracked(path, session):
                logger.info("skipping %s: resolved Not Tracked, not auto-enqueuing", path)
            return None

        if not detect_qualifying_targets(streams, effective_settings):
            return None

        with self._enqueue_lock:
            if self._is_duplicate(path, session):
                return None
            return self._queue.enqueue(path, effective_settings)

    def _track_media(
        self,
        path: Path,
        streams: Sequence[AudioStreamInfo],
        settings: DownmixSettings,
        session: Session | None,
        *,
        instance_id: int | None = None,
        sonarr_episode_id: int | None = None,
        radarr_movie_id: int | None = None,
    ) -> None:
        """Upsert ``path``'s tracked-media row from ``streams`` (COL-95/COL-101).

        Mirrors :meth:`_is_duplicate`'s session handling: reuses ``session``
        when the caller passed one (:meth:`scan_once` does, since it already
        has one open for the whole scan), else opens a short-lived one via
        :attr:`_session_factory` (the webhook and manual-trigger paths, which
        don't have one open).
        """
        if session is not None:
            upsert_tracked_media(
                session,
                file_path=path,
                streams=streams,
                settings=settings,
                instance_id=instance_id,
                sonarr_episode_id=sonarr_episode_id,
                radarr_movie_id=radarr_movie_id,
            )
            return
        with self._session_factory() as owned_session:
            upsert_tracked_media(
                owned_session,
                file_path=path,
                streams=streams,
                settings=settings,
                instance_id=instance_id,
                sonarr_episode_id=sonarr_episode_id,
                radarr_movie_id=radarr_movie_id,
            )

    def _resolve_tracked(
        self,
        session: Session | None,
        *,
        instance_id: int | None,
        sonarr_episode_id: int | None,
        radarr_movie_id: int | None,
        path: Path | None = None,
    ) -> bool:
        """Resolve the file's effective **Tracked** value for the auto-enqueue gate.

        Returns ``True`` (proceed) when there is no catalog identity to gate on
        -- an ``instance_id`` plus an episode *or* movie id is required to bridge
        the file to its :class:`~collapsarr.library.models.LibraryNode`; without
        one (a bare-path manual trigger) the file is treated as Tracked, the
        pre-COL-102 behavior. With ids present but no matching node, falls back
        to ``GlobalSettings.default_tracked`` -- the same fallback
        :func:`~collapsarr.library.service.resolve_tracked` itself uses when
        nothing in a node's ancestry is explicit; this fallback is logged (COL-134)
        via ``path`` (when the caller has one) so it's diagnosable from server
        logs whether a file shown/skipped this way is genuinely Tracked or the
        bridge simply couldn't find a node. Mirrors :meth:`_is_duplicate`'s
        session handling: reuses ``session`` when the caller has one open (the
        scan), else opens a short-lived one (the webhook/manual paths).
        """
        if instance_id is None or (sonarr_episode_id is None and radarr_movie_id is None):
            return True
        if session is not None:
            return self._resolve_tracked_in(
                session,
                instance_id=instance_id,
                sonarr_episode_id=sonarr_episode_id,
                radarr_movie_id=radarr_movie_id,
                path=path,
            )
        with self._session_factory() as owned_session:
            return self._resolve_tracked_in(
                owned_session,
                instance_id=instance_id,
                sonarr_episode_id=sonarr_episode_id,
                radarr_movie_id=radarr_movie_id,
                path=path,
            )

    def _resolve_tracked_in(
        self,
        session: Session,
        *,
        instance_id: int,
        sonarr_episode_id: int | None,
        radarr_movie_id: int | None,
        path: Path | None = None,
    ) -> bool:
        """Resolve Tracked for a file with catalog ids, within ``session``."""
        default_tracked = get_global_settings(session).default_tracked
        node = get_node_by_source_id(
            session,
            instance_id=instance_id,
            sonarr_episode_id=sonarr_episode_id,
            radarr_movie_id=radarr_movie_id,
        )
        if node is None:
            if path is None or self._should_log_bridge_missing(path, session):
                logger.warning(
                    "tracked bridge: no LibraryNode for instance_id=%s "
                    "sonarr_episode_id=%s radarr_movie_id=%s (%s) -- "
                    "falling back to default_tracked=%s",
                    instance_id,
                    sonarr_episode_id,
                    radarr_movie_id,
                    path,
                    default_tracked,
                )
            return default_tracked
        nodes_by_id = {n.id: n for n in list_nodes(session, instance_id)}
        return resolve_tracked(node, nodes_by_id, default_tracked)

    def trigger_file(
        self,
        file_path: str | Path,
        *,
        extra_languages: Iterable[str] | None = None,
        session: Session | None = None,
    ) -> Job | None:
        """Manually trigger a downmix job for one file on demand (COL-23).

        The entry point a future "trigger this file" API/UI action calls
        (COL-29) -- unlike the automatic triggers (:meth:`on_file_ready`,
        :meth:`scan_once`), which always enqueue against the scheduler's fixed
        ``self._downmix_settings``, this lets the caller pass
        ``extra_languages`` to reach languages the scheduler's
        ``language_allow_list`` would otherwise exclude, for the
        manual-override use case (e.g. a user wants a language downmixed just
        this once even though it's not in the global allow-list).

        ``extra_languages`` is unioned onto ``self._downmix_settings.language_allow_list``
        for this call only:

        - If that allow-list is ``None`` (no restriction -- every language is
          already evaluated), ``extra_languages`` has no effect.
        - If it is a concrete set, the languages named in ``extra_languages``
          are unioned in just for this trigger; ``self._downmix_settings``
          itself, and every other trigger, are unaffected.

        Bypasses the **Tracked** gate (COL-102): a manual trigger is an
        explicit user action, so it downmixes even a Not-Tracked file (Tracked
        gates only *automatic* enqueueing -- ``CONTEXT.md``). It still goes
        through the same dedup and qualifying-target detection as the automatic
        triggers -- the acceptance criteria ask to bypass the *language
        allow-list* (and Tracked) specifically, not dedup or "does this file
        actually need downmixing". Returns the created
        :class:`~collapsarr.jobs.queue.Job`, or ``None`` for the same reasons
        :meth:`enqueue_file` would (duplicate, unprobeable, or still no
        qualifying target even with the extra languages included).
        """
        settings = self._settings_with_extra_languages(extra_languages)
        return self.enqueue_file(
            file_path, session=session, settings=settings, respect_tracked=False
        )

    def _settings_with_extra_languages(
        self, extra_languages: Iterable[str] | None
    ) -> DownmixSettings:
        """``self._downmix_settings`` with ``extra_languages`` unioned onto its allow-list."""
        allow_list = self._downmix_settings.language_allow_list
        extra = frozenset(extra_languages) if extra_languages is not None else frozenset()
        if not extra or allow_list is None:
            return self._downmix_settings
        return replace(self._downmix_settings, language_allow_list=allow_list | extra)

    def trigger_set_default_audio(
        self,
        file_path: str | Path,
        *,
        session: Session | None = None,
    ) -> Job | None:
        """Manually trigger a Default Audio Track fix job for one file on demand (COL-155).

        The entry point the single-file "set default audio track" REST
        endpoint (:mod:`collapsarr.jobs.routes`) calls. Unlike
        :meth:`trigger_file` (which delegates to :meth:`enqueue_file`), this
        has its own probe/decide/enqueue sequence -- the "does this file need
        anything" question is a different algorithm
        (:func:`~collapsarr.downmix.default_audio.resolve_default_audio_stream`
        over the *existing* disposition, not
        :func:`~collapsarr.downmix.targets.detect_qualifying_targets` over
        missing downmix targets):

        1. Resolve the persisted **Preferred Default Audio** setting
           (:func:`~collapsarr.settings.service.as_default_audio_preference`).
           Returns ``None`` -- nothing to act on -- if either half of the
           preference (language / channel tier) is unset; unlike COL-153's
           pipeline (an explicit per-call ``preference`` argument), a manual
           trigger's preference always comes from persisted settings, so an
           unset one really does mean there's nothing to compare against.
        2. De-duplication (shared with every ``DOWNMIX`` trigger --
           :meth:`_is_duplicate` matches purely on file path, oblivious to
           kind, so an in-flight job of *either* kind for this file blocks
           the other -- see the module docstring).
        3. Probe the file's audio streams. A probe failure is logged and
           skipped, same as :meth:`enqueue_file`.
        4. Resolve the disposition winner. Returns ``None`` when there are
           fewer than two streams to compare, or when the winner already --
           and solely -- carries the disposition (nothing to change).
        5. Re-check de-duplication under :attr:`_enqueue_lock` (closing the
           same race window :meth:`enqueue_file` closes) and enqueue via
           :meth:`~collapsarr.jobs.queue.JobQueue.enqueue_default_audio`.

        Bypasses the **Tracked** gate unconditionally, the same rationale as
        :meth:`trigger_file`: a manual trigger is an explicit user action
        (CONTEXT.md's Tracked gates *automatic* enqueueing only). Does
        **not** call :meth:`_track_media` -- Default Audio Track disposition
        is orthogonal to the downmix-target tracking
        :func:`~collapsarr.media.service.upsert_tracked_media` maintains for
        the Wanted view, so there is nothing of that shape to record here.

        Returns the created :class:`~collapsarr.jobs.queue.Job`, or ``None``
        for any of the "nothing to do" reasons above (no preference
        configured, duplicate, unprobeable, or the file already correct).
        """
        path = Path(file_path)

        preference = self._resolve_default_audio_preference(session)
        if preference is None:
            logger.info(
                "skipping %s: no Default Audio Track preference configured", path
            )
            return None

        if self._is_duplicate(path, session):
            return None

        try:
            streams = self._probe(path)
        except FfprobeError as exc:
            logger.warning("skipping %s: could not probe audio streams: %s", path, exc)
            return None

        winner = resolve_default_audio_stream(streams, preference)
        if winner is None or self._default_audio_already_correct(streams, winner):
            return None

        with self._enqueue_lock:
            if self._is_duplicate(path, session):
                return None
            return self._queue.enqueue_default_audio(path, preference)

    def _resolve_default_audio_preference(
        self, session: Session | None
    ) -> DefaultAudioPreference | None:
        """Adapt the persisted Preferred Default Audio setting, opening a session if needed.

        Mirrors :meth:`_is_duplicate`'s session handling: reuses ``session``
        when the caller has one open, else opens a short-lived one.
        """
        if session is not None:
            return as_default_audio_preference(get_global_settings(session))
        with self._session_factory() as owned_session:
            return as_default_audio_preference(get_global_settings(owned_session))

    @staticmethod
    def _default_audio_already_correct(
        streams: Sequence[AudioStreamInfo], winner: AudioStreamInfo
    ) -> bool:
        """Whether ``winner`` already -- and solely -- carries the Default Audio Track disposition.

        Mirrors :func:`~collapsarr.downmix.default_audio_pipeline._already_correct`
        (a private helper of that module, deliberately not imported here --
        the check is two lines and this module already has ``streams`` in
        the exact shape it needs, no index lookup required).
        """
        return winner.is_default and not any(
            stream.is_default for stream in streams if stream is not winner
        )

    def _is_duplicate(self, path: Path, session: Session | None) -> bool:
        """Whether ``path`` is already queued/running or was recently processed."""
        if self._is_active(path):
            return True
        if session is not None:
            return self._is_recently_processed(path, session)
        with self._session_factory() as owned_session:
            return self._is_recently_processed(path, owned_session)

    def _is_active(self, path: Path) -> bool:
        """Whether a job for ``path`` is currently ``PENDING`` or ``RUNNING``."""
        return any(
            job.file_path == path and job.status in _ACTIVE_STATUSES
            for job in self._queue.list_jobs()
        )

    def _dedup_window_minutes(self, session: Session | None) -> int:
        """Read ``GlobalSettings.recently_processed_window_minutes`` live (COL-167).

        Deliberately **not** cached on ``self`` -- read fresh from the settings
        service on every call, so a ``PUT /api/settings`` change is visible on
        the very next dedup check with no restart or scheduler reconstruction.
        Mirrors :meth:`_is_duplicate`'s session handling: reuses ``session``
        when the caller has one open, else opens a short-lived one.
        """
        if session is not None:
            return get_global_settings(session).recently_processed_window_minutes
        with self._session_factory() as owned_session:
            return get_global_settings(owned_session).recently_processed_window_minutes

    def _should_log_once(
        self, cache: dict[Path, datetime], path: Path, session: Session | None
    ) -> bool:
        """Whether to log ``path`` now against ``cache``, or suppress a repeat.

        Logged once per file, then suppressed until the live dedup window
        (:meth:`_dedup_window_minutes`) elapses -- the same window
        :meth:`_is_recently_processed` uses -- so a persistently-recurring
        condition doesn't spam one identical line per scan forever, while a
        dedup window later a fresh line still confirms it's still true
        (rather than going silent permanently). A window of ``0`` (cooldown
        disabled) means every call re-logs, matching the "no cooldown"
        semantics elsewhere. ``cache`` lets callers track distinct log
        conditions (COL-135's Not-Tracked skip, COL-134's bridge-missing
        fallback) independently -- one firing never suppresses the other for
        the same file.
        """
        window = timedelta(minutes=self._dedup_window_minutes(session))
        now = self._now()
        with self._not_tracked_log_lock:
            last = cache.get(path)
            if last is not None and now - last < window:
                return False
            cache[path] = now
            return True

    def _should_log_not_tracked(self, path: Path, session: Session | None) -> bool:
        """Whether to log ``path``'s Not-Tracked skip now (COL-135)."""
        return self._should_log_once(self._not_tracked_logged, path, session)

    def _should_log_bridge_missing(self, path: Path, session: Session | None) -> bool:
        """Whether to log ``path``'s bridge-missing fallback now (COL-134)."""
        return self._should_log_once(self._bridge_missing_logged, path, session)

    def _is_recently_processed(self, path: Path, session: Session) -> bool:
        """Whether a terminal history row for ``path`` falls inside the live dedup window.

        Reads ``GlobalSettings.recently_processed_window_minutes`` live via
        :func:`~collapsarr.settings.service.get_global_settings` on every call
        (COL-167) rather than a value cached at :meth:`__init__` -- see the
        module docstring. ``0`` short-circuits to "never recently processed":
        every file is always eligible for retry.
        """
        minutes = get_global_settings(session).recently_processed_window_minutes
        if minutes <= 0:
            return False
        cutoff = self._now() - timedelta(minutes=minutes)
        for row in list_job_history(session, file_path=str(path)):
            if row.status not in _TERMINAL_STATUSES or row.ended_at is None:
                continue
            ended = row.ended_at
            if ended.tzinfo is None:  # SQLite round-trips datetimes as naive UTC.
                ended = ended.replace(tzinfo=UTC)
            if ended >= cutoff:
                return True
        return False

    # -- Periodic full-library scan -----------------------------------------

    def scan_now(self) -> list[Job]:
        """Manually trigger a full-library scan immediately (COL-23).

        An intention-revealing alias for :meth:`scan_once` -- the entry point
        a future "Scan now" API/UI action calls (COL-29). The scan already
        runs synchronously and doesn't depend on the background loop being
        started, so no new scan logic is needed here; this just gives the
        manual-trigger use case its own named method rather than requiring
        callers to know :meth:`scan_once` (the periodic loop's internal
        entry point) doubles as the manual one. Returns the jobs enqueued by
        this pass, same as :meth:`scan_once`.
        """
        return self.scan_once()

    def scan_once(self) -> list[Job]:
        """Scan every configured instance: mirror its Library and enqueue qualifying files.

        Two independent per-instance passes share the one scan cadence:

        - **Library mirror (COL-98/COL-99):** for each Sonarr instance, fetch
          its full catalog (:attr:`_catalog_fetch`); for each Radarr instance,
          fetch its full movie catalog (:attr:`_radarr_catalog_fetch`); then
          :func:`~collapsarr.library.service.sync_library` -- upserting every
          Series/Season/Episode or Movie node (files-not-yet-present included)
          and soft-hiding any node the catalog no longer reports.
        - **Downmix discovery (COL-22):** enqueue every monitored file with a
          qualifying missing target.

        Returns the jobs enqueued this pass (skipped/no-op files excluded). A
        fetch failure for one instance -- for either pass -- is logged and
        skipped rather than aborting the whole scan, so one unreachable
        Sonarr/Radarr doesn't stop the others (or the other pass) from running.

        Stamps :attr:`last_scan_at` (COL-122) at the very start, before any
        instance is synced -- so it reflects when this pass *started*, and is
        set even if the pass later fails partway through fetching some
        instance's catalog/files.
        """
        self._last_scan_at = self._now()
        enqueued: list[Job] = []
        with self._session_factory() as session:
            instances = list_instances(session)
            for instance in instances:
                self._sync_instance_library(session, instance)

                try:
                    files = fetch_monitored_files(instance)
                except httpx.HTTPError as exc:
                    logger.warning(
                        "scan: failed to fetch files from instance %r (id=%s): %s",
                        instance.name,
                        instance.id,
                        exc,
                    )
                    continue
                mappings = list_path_mappings(session, instance.id)
                for monitored in files:
                    local_path = resolve_path(monitored.file_path, mappings)
                    job = self.enqueue_file(
                        local_path,
                        session=session,
                        instance_id=monitored.instance_id,
                        sonarr_episode_id=monitored.sonarr_episode_id,
                        radarr_movie_id=monitored.radarr_movie_id,
                    )
                    if job is not None:
                        enqueued.append(job)
        logger.info(
            "scan complete: enqueued %d job(s) across %d instance(s)",
            len(enqueued),
            len(instances),
        )
        return enqueued

    def _sync_instance_library(self, session: Session, instance: ArrInstance) -> None:
        """Mirror one configured instance's catalog into the Library (COL-98/COL-99).

        Dispatches on ``instance.type``: a Sonarr instance's full
        Series/Season/Episode catalog is fetched via :attr:`_catalog_fetch`, a
        Radarr instance's full movie catalog via :attr:`_radarr_catalog_fetch`
        -- both then upserted through the same
        :func:`~collapsarr.library.service.sync_library` entry point. A
        catalog-fetch failure -- a network/HTTP-status failure
        (``httpx.HTTPError``) or an HTTP 200 with an unexpected body shape
        (:class:`~collapsarr.arr.catalog.MalformedCatalogResponse`, COL-136)
        -- is logged and swallowed so it never aborts the scan. Crucially,
        ``sync_library`` is *not* called on either failure, so neither a
        transient outage nor a malformed-but-200 response ever soft-hides
        the whole mirror.
        """
        catalog: SonarrCatalog | RadarrCatalog
        try:
            if instance.type is InstanceType.SONARR:
                catalog = self._catalog_fetch(instance)
            elif instance.type is InstanceType.RADARR:
                catalog = self._radarr_catalog_fetch(instance)
            else:  # pragma: no cover - InstanceType has exactly two members
                return
        except (httpx.HTTPError, MalformedCatalogResponse) as exc:
            logger.warning(
                "scan: failed to fetch catalog from instance %r (id=%s): %s",
                instance.name,
                instance.id,
                exc,
            )
            return
        sync_library(session, instance_id=instance.id, catalog=catalog)

    # -- Background loop lifecycle ------------------------------------------

    def start(self) -> None:
        """Start the background scan loop in a daemon thread.

        Runs an initial scan immediately, then repeats every
        ``scan_interval_hours``. Idempotency is the caller's responsibility --
        calling this twice raises.
        """
        if self._thread is not None:
            raise RuntimeError("JobScheduler is already started")
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run, name="collapsarr-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float | None = _STOP_JOIN_TIMEOUT) -> None:
        """Signal the loop to stop and join its thread (a no-op if not started)."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        """Sleep/wake loop: run a full scan on the interval, then wait for the next one.

        No longer drains the queue itself: since COL-164 the
        :class:`~collapsarr.jobs.queue.JobQueue` runs a persistent worker pool,
        so a job starts running the instant :meth:`scan_once` (or a webhook)
        enqueues it -- there is no batch for this loop to kick off. The loop's
        sole remaining job is the periodic scan; ``_wake`` now serves only to
        break the wait promptly on :meth:`stop`.
        """
        next_scan = time.monotonic()  # scan immediately on the first iteration
        while not self._stop.is_set():
            if time.monotonic() >= next_scan:
                try:
                    self.scan_once()
                except Exception:  # noqa: BLE001 - one bad scan must not kill the loop
                    logger.exception("scheduled library scan failed")
                next_scan = time.monotonic() + self._interval_seconds
            if self._stop.is_set():
                break
            self._wake.wait(timeout=max(0.0, next_scan - time.monotonic()))
            self._wake.clear()
