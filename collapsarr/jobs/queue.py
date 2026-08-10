"""Job queue and bounded-concurrency worker pool (COL-20).

Wires the Downmix Engine's end-to-end pipeline
(:func:`~collapsarr.downmix.pipeline.run_downmix_pipeline`, COL-19) to a job
queue: :meth:`JobQueue.enqueue` a file plus its target/language context (a
:class:`~collapsarr.downmix.targets.DownmixSettings`), then
:meth:`JobQueue.run_pending` drains the queue, running at most
``max_concurrency`` jobs at once (default 1).

Each job's execution invokes the pipeline synchronously in a worker thread
and captures whatever it returns (or, as a safety net, whatever it raises)
onto the :class:`Job` itself -- ``status`` plus ``result``/``error``. When a
``history_recorder`` is configured (see :class:`JobQueue`), it is called at
every lifecycle stage -- immediately on :meth:`JobQueue.enqueue` (``PENDING``),
again as the worker thread picks the job up (``RUNNING``), and again once it
reaches a terminal status -- so every job is visible in job history (COL-21,
:mod:`collapsarr.jobs.history`) from the moment it's queued, not only once it
finishes (COL-108), without the caller having to remember to call it itself.
Likewise, when a ``failure_notifier`` is
configured, that same worker thread calls it -- but only for a job that
reached ``FAILED`` -- so a downmix failure fans out to the configured
notifiers (COL-37, :mod:`collapsarr.jobs.failure_notify`) without the
caller having to remember to do it either. And when a ``tracked_media_recorder``
is configured, that same worker thread calls it -- but only for a job that
reached ``SUCCEEDED`` -- so the file's just-processed ``(language, target)``
pairs flip to ``PROCESSED`` in tracked media (COL-95,
:mod:`collapsarr.jobs.tracked_media`), the Wanted view's data source,
immediately rather than only after the next scan re-probes the file.

This module deliberately does not import :mod:`collapsarr.jobs.history`,
:mod:`collapsarr.jobs.failure_notify`, or :mod:`collapsarr.jobs.tracked_media`
itself (those modules import *this* one, for :class:`Job`/:class:`JobStatus`
-- importing them back here would be circular). Instead
``history_recorder``/``failure_notifier``/``tracked_media_recorder`` are
plain injected callables, the same seam ``pipeline_runner`` already uses;
:func:`collapsarr.jobs.history.make_history_recorder`,
:func:`collapsarr.jobs.failure_notify.make_failure_notifier`, and
:func:`collapsarr.jobs.tracked_media.make_tracked_media_recorder` build ones
bound to a session factory.

Threads, not asyncio: every stage of the downmix pipeline shells out to
``ffprobe``/``ffmpeg`` via blocking :mod:`subprocess` calls, so a small
:class:`~concurrent.futures.ThreadPoolExecutor` sized to ``max_concurrency``
gives genuine bounded parallelism (the GIL is released for the whole
``subprocess.run`` call) without pulling the rest of this synchronous
codebase onto an event loop.

``max_concurrency`` is a plain constructor argument, the same "Settings-
shaped stand-in" pattern :class:`~collapsarr.downmix.targets.DownmixSettings`
already uses -- there is no persisted Settings model yet. It defaults to 1,
and :meth:`JobQueue.from_settings` sources it from
:class:`~collapsarr.config.Settings`'s ``job_max_concurrency`` (env
``COLLAPSARR_JOB_MAX_CONCURRENCY``), which is the closest thing this repo has
to a Settings store today.

:meth:`JobQueue._run_job` also logs the job lifecycle (COL-129), via a
module logger -- so it lands in the rotating log file COL-128 wires up: an
``INFO`` line when the job starts (job id, file path, enabled targets) and
another when it completes successfully. The pipeline itself
(:mod:`collapsarr.downmix.pipeline`) already logs ``WARNING``/``ERROR`` for
its own non-success outcomes, so this module logs an ``ERROR`` of its own
only for the one failure shape only it can observe -- ``pipeline_runner``
raising unexpectedly rather than returning a :class:`~collapsarr.downmix.
pipeline.PipelineResult` -- to avoid a duplicate log line for the common
case where the pipeline already reported its own failure.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from collapsarr.config import Settings, get_settings
from collapsarr.downmix.pipeline import PipelineResult, run_downmix_pipeline
from collapsarr.downmix.targets import DownmixSettings

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENCY = 1

#: Signature every pipeline runner (real or injected-for-tests) must match:
#: ``(file_path, settings, **pipeline_kwargs) -> PipelineResult``.
PipelineRunner = Callable[..., PipelineResult]


def _enabled_targets_for_log(settings: DownmixSettings) -> str:
    """Render ``settings.enabled_targets`` for a log line, in a stable order."""
    return ",".join(sorted(target.value for target in settings.enabled_targets))


class JobStatus(Enum):
    """Lifecycle state of a single :class:`Job`."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(slots=True)
class Job:
    """One enqueued unit of work: a file plus its downmix target/language context.

    ``id`` uniquely identifies the job -- COL-21's job-history layer
    (:mod:`collapsarr.jobs.history`) persists against it as ``job_id``.
    ``status``, ``result``/``error``, and ``started_at``/``ended_at`` start
    empty and are filled in by the queue as the job runs -- never mutate
    them directly.

    ``result`` carries the pipeline's :class:`~collapsarr.downmix.pipeline.PipelineResult`
    when the pipeline ran (success, no-op, or a captured failure at any
    stage). ``error`` is populated instead only in the unexpected case where
    the pipeline runner itself raised rather than returning a result (the
    real pipeline never does this -- see its own docstring -- but an
    injected runner in a test, or a future alternate runner, might).

    ``started_at``/``ended_at`` are stamped (UTC) by :meth:`JobQueue._run_job`
    when the job transitions to ``RUNNING`` and when it reaches a terminal
    status, respectively -- the start/end timestamps COL-21's job history
    persists. Both stay ``None`` for a job that has never run.
    """

    file_path: Path
    settings: DownmixSettings
    id: UUID = field(default_factory=uuid4)
    status: JobStatus = JobStatus.PENDING
    result: PipelineResult | None = None
    error: BaseException | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


#: Signature a ``history_recorder`` must match: takes ``Job`` at its current
#: lifecycle stage (``PENDING``/``RUNNING``/terminal) and persists it (see
#: :func:`collapsarr.jobs.history.make_history_recorder`).
HistoryRecorder = Callable[[Job], None]

#: Signature a ``failure_notifier`` must match: takes the just-terminated
#: ``Job`` (only ever called for one with ``status is JobStatus.FAILED``) and
#: dispatches a notification for it (see :func:`collapsarr.jobs.
#: failure_notify.make_failure_notifier`). Must never raise -- see
#: :meth:`JobQueue._notify_failure`.
FailureNotifier = Callable[[Job], None]

#: Signature a ``tracked_media_recorder`` must match: takes the just-terminated
#: ``Job`` (only ever called for one with ``status is JobStatus.SUCCEEDED``)
#: and flips its processed ``(language, target)`` pairs to ``PROCESSED`` in
#: tracked media (see :func:`collapsarr.jobs.tracked_media.
#: make_tracked_media_recorder`), so a file's now-satisfied target stops
#: appearing in the Wanted view without waiting for the next scan (COL-95).
TrackedMediaRecorder = Callable[[Job], None]


class JobQueue:
    """Bounded-concurrency queue that runs the downmix pipeline per enqueued file.

    Usage::

        queue = JobQueue(max_concurrency=2)
        queue.enqueue("/media/movie.mkv", DownmixSettings())
        queue.enqueue("/media/episode.mkv", DownmixSettings())
        jobs = queue.run_pending()  # blocks until both have run

    :meth:`run_pending` snapshots whatever is pending at the moment it is
    called and runs exactly that batch, respecting ``max_concurrency``, then
    returns those jobs (each updated in place with its final ``status`` and
    ``result``). Jobs enqueued *during* a call are not picked up by it --
    call :meth:`run_pending` again for a later batch. This keeps behaviour
    simple and fully deterministic for tests; a long-running background
    worker loop is left for a future scheduler ticket to build on top of
    this primitive.

    ``history_recorder``, when set, is called with each :class:`Job` three
    times over its lifecycle: immediately on :meth:`enqueue` (``PENDING``,
    from whichever thread called ``enqueue``), then from the worker thread
    that runs it as it transitions to ``RUNNING``, and again right after it
    reaches a terminal status (``SUCCEEDED``/``FAILED``) -- so a job is
    visible in job history the instant it's queued, not only once it
    finishes (COL-108). See :func:`collapsarr.jobs.history.
    make_history_recorder` for the constructor that builds one bound to a
    real DB session factory. Since :meth:`run_pending` runs jobs across a
    :class:`~concurrent.futures.ThreadPoolExecutor` (up to ``max_concurrency``
    at once), ``history_recorder`` must itself be safe to call concurrently
    from multiple threads; :func:`~collapsarr.jobs.history.
    make_history_recorder` satisfies this by opening a fresh
    :class:`~sqlalchemy.orm.Session` per call rather than sharing one --
    SQLAlchemy sessions aren't thread-safe, but a ``sessionmaker`` safely
    creates independent sessions from any thread.

    ``failure_notifier``, when set, is called the same way -- same worker
    thread, right after the job reaches its terminal status -- but only for
    a job whose terminal status is ``FAILED`` (a ``SUCCEEDED`` job never
    triggers it). See :func:`collapsarr.jobs.failure_notify.
    make_failure_notifier` for the constructor that dispatches to the
    configured notifiers (COL-37); it must be safe to call concurrently for
    the same reason ``history_recorder`` must, and -- like
    ``history_recorder`` -- any exception it raises is swallowed by
    :meth:`_notify_failure` so a notifier problem can never fail the job it
    is reporting on.

    ``tracked_media_recorder``, when set, is called the same way -- same
    worker thread, right after the job reaches its terminal status -- but
    only for a job whose terminal status is ``SUCCEEDED`` (COL-95). See
    :func:`collapsarr.jobs.tracked_media.make_tracked_media_recorder` for
    the constructor that flips the job's actually-processed
    ``(language, target)`` pairs to ``PROCESSED`` in tracked media, so the
    Wanted view (:mod:`collapsarr.media.routes`) reflects a successful
    downmix immediately rather than only after the next scan re-probes the
    file; it must be safe to call concurrently for the same reason
    ``history_recorder``/``failure_notifier`` must.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        pipeline_runner: PipelineRunner = run_downmix_pipeline,
        pipeline_kwargs: Mapping[str, Any] | None = None,
        history_recorder: HistoryRecorder | None = None,
        failure_notifier: FailureNotifier | None = None,
        tracked_media_recorder: TrackedMediaRecorder | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")
        self._max_concurrency = max_concurrency
        self._pipeline_runner = pipeline_runner
        self._pipeline_kwargs = dict(pipeline_kwargs or {})
        self._history_recorder = history_recorder
        self._failure_notifier = failure_notifier
        self._tracked_media_recorder = tracked_media_recorder
        self._lock = threading.Lock()
        self._jobs: dict[UUID, Job] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        pipeline_runner: PipelineRunner = run_downmix_pipeline,
        pipeline_kwargs: Mapping[str, Any] | None = None,
        history_recorder: HistoryRecorder | None = None,
        failure_notifier: FailureNotifier | None = None,
        tracked_media_recorder: TrackedMediaRecorder | None = None,
    ) -> JobQueue:
        """Build a :class:`JobQueue` whose concurrency cap comes from Settings.

        ``settings`` defaults to the process-wide cached
        :func:`~collapsarr.config.get_settings`. Its ``job_max_concurrency``
        (default 1, env ``COLLAPSARR_JOB_MAX_CONCURRENCY``) becomes
        ``max_concurrency``.

        Unlike the raw :meth:`__init__` (where ``history_recorder``/
        ``failure_notifier``/``tracked_media_recorder`` default to ``None``
        -- the right default for lightweight unit construction that doesn't
        want DB writes, e.g. COL-20's concurrency tests), this factory is
        the production path: when any isn't passed explicitly, it defaults
        to a *real* one -- ``history_recorder`` via :func:`collapsarr.jobs.
        history.make_history_recorder`, ``failure_notifier`` via
        :func:`collapsarr.jobs.failure_notify.make_failure_notifier`, and
        ``tracked_media_recorder`` via :func:`collapsarr.jobs.tracked_media.
        make_tracked_media_recorder` -- all three bound to the same session
        factory for ``resolved``'s database (schema brought up to head via
        :func:`~collapsarr.migrations.upgrade_to_head` if not already
        current) -- rather than staying ``None``. This mirrors how
        ``pipeline_runner`` already defaults to the real
        :func:`~collapsarr.downmix.pipeline.run_downmix_pipeline` in the raw
        ``__init__``: a bare ``JobQueue.from_settings()`` call, with no extra
        plumbing, persists history, dispatches failure notifications, and
        keeps tracked media (the Wanted view's data source, COL-95) up to
        date for real. Pass any of the three explicitly (or ``None`` isn't
        obtainable here -- construct via :meth:`__init__` directly instead)
        to opt out.

        This factory is also where the persisted **Preferred Default Audio**
        settings reach a real downmix job (COL-152): it reads
        ``GlobalSettings.auto_set_default_audio`` and, when that opt-in toggle
        is on, folds ``auto_set_default_audio=True`` plus the adapted
        ``default_audio_preference`` (from
        ``GlobalSettings.default_audio_language``/``default_audio_channel_tier``)
        into the ``pipeline_kwargs`` every enqueued job passes to
        :func:`~collapsarr.downmix.pipeline.run_downmix_pipeline` -- so the
        automatic in-band Default Audio Track fix is genuinely reachable from a
        real job dispatched through this queue, not only from the pipeline
        function's parameter surface. With the toggle off (the default) nothing
        is added and a job runs byte-for-byte as it did before the feature; an
        explicit ``pipeline_kwargs`` key from the caller always wins. See
        :meth:`_resolve_pipeline_kwargs`.

        The database engine backing all of the above is created at most once,
        here, for this :class:`JobQueue` instance -- shared between
        ``history_recorder``, ``failure_notifier``,
        ``tracked_media_recorder``, and the Default-Audio settings read, but
        not shared with the FastAPI app's own request-scoped engine (see
        :mod:`collapsarr.main`). For SQLite (this project's only supported
        backend today) that's safe -- both point at the same on-disk file --
        but it does mean calling this factory repeatedly opens a new engine
        each time, so production code should call it once and hold onto the
        resulting :class:`JobQueue` (e.g. on ``app.state``), the same way it
        already holds onto one session factory.

        The imports of :mod:`collapsarr.jobs.history`,
        :mod:`collapsarr.jobs.failure_notify`, and :mod:`collapsarr.jobs.
        tracked_media` below are deferred (inside this method, not at module
        scope) because those modules import *this* one (for
        :class:`Job`/:class:`JobStatus`) -- a deferred import to break the
        module cycle, the same reason the schema/engine helpers above are
        imported inside this method rather than at module scope.
        """
        resolved = settings or get_settings()

        from collapsarr.database import (
            create_engine_from_settings,
            create_session_factory,
        )
        from collapsarr.migrations import upgrade_to_head

        upgrade_to_head(resolved)
        engine = create_engine_from_settings(resolved)
        session_factory = create_session_factory(engine)

        resolved_history_recorder = history_recorder
        if resolved_history_recorder is None:
            from collapsarr.jobs.history import make_history_recorder

            resolved_history_recorder = make_history_recorder(session_factory)

        resolved_failure_notifier = failure_notifier
        if resolved_failure_notifier is None:
            from collapsarr.jobs.failure_notify import make_failure_notifier

            resolved_failure_notifier = make_failure_notifier(session_factory)

        resolved_tracked_media_recorder = tracked_media_recorder
        if resolved_tracked_media_recorder is None:
            from collapsarr.jobs.tracked_media import make_tracked_media_recorder

            resolved_tracked_media_recorder = make_tracked_media_recorder(session_factory)

        return cls(
            max_concurrency=resolved.job_max_concurrency,
            pipeline_runner=pipeline_runner,
            pipeline_kwargs=cls._resolve_pipeline_kwargs(pipeline_kwargs, session_factory),
            history_recorder=resolved_history_recorder,
            failure_notifier=resolved_failure_notifier,
            tracked_media_recorder=resolved_tracked_media_recorder,
        )

    @staticmethod
    def _resolve_pipeline_kwargs(
        pipeline_kwargs: Mapping[str, Any] | None,
        session_factory: sessionmaker[Session],
    ) -> dict[str, Any]:
        """Fold the persisted Default Audio Track preference into ``pipeline_kwargs`` (COL-152).

        Reads the singleton :class:`~collapsarr.settings.models.GlobalSettings`
        row from ``session_factory`` (the same DB the recorders above bind to)
        and, **only** when its opt-in ``auto_set_default_audio`` toggle is on,
        threads ``auto_set_default_audio=True`` plus the adapted
        ``default_audio_preference`` (:func:`~collapsarr.settings.service.
        as_default_audio_preference`) into the kwargs every enqueued downmix job
        passes to :func:`~collapsarr.downmix.pipeline.run_downmix_pipeline`. This
        is the production wiring that makes the automatic in-band fix reachable:
        a real downmix job dispatched through this queue now actually applies it.

        With the toggle off (the default, every fresh install's state) nothing is
        added, so a job's pipeline call is byte-for-byte what it was before this
        feature. An explicit ``pipeline_kwargs`` from the caller always wins --
        keys already present are never overwritten -- so a test (or a future
        alternate wiring) can still pin its own values.

        The imports below are deferred, matching the surrounding factory: the
        settings service pulls in the ORM/adapters, which don't need to load for
        a lightweight :meth:`__init__` construction that never touches Settings.
        """
        from collapsarr.settings.service import (
            as_default_audio_preference,
            get_global_settings,
        )

        resolved = dict(pipeline_kwargs or {})
        with session_factory() as session:
            global_settings = get_global_settings(session)
        if global_settings.auto_set_default_audio:
            resolved.setdefault("auto_set_default_audio", True)
            resolved.setdefault(
                "default_audio_preference",
                as_default_audio_preference(global_settings),
            )
        return resolved

    @property
    def max_concurrency(self) -> int:
        """The configured cap on simultaneously running jobs."""
        return self._max_concurrency

    def enqueue(self, file_path: str | Path, settings: DownmixSettings) -> Job:
        """Add a file + its target/language context to the queue as a new job.

        Returns the created :class:`Job` (status ``PENDING``) immediately;
        it is not run until a subsequent :meth:`run_pending` call. Persisted
        via ``history_recorder`` (if configured) right away, so a job shows up
        in job history -- and so the Activity view -- the instant it's
        queued, rather than only once it finishes (COL-108).
        """
        job = Job(file_path=Path(file_path), settings=settings)
        with self._lock:
            self._jobs[job.id] = job
        self._record_history(job)
        return job

    def get_job(self, job_id: UUID) -> Job | None:
        """Return the job with ``job_id``, or ``None`` if no such job exists."""
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        """Return every job ever enqueued on this queue, in enqueue order."""
        with self._lock:
            return list(self._jobs.values())

    def run_pending(self) -> list[Job]:
        """Run every currently-``PENDING`` job to completion, then return them.

        Runs the batch through a :class:`~concurrent.futures.ThreadPoolExecutor`
        sized to ``max_concurrency``, so at most that many jobs execute the
        pipeline at once; with ``max_concurrency=1`` the executor has a
        single worker, so jobs run strictly one at a time, in the order they
        were enqueued.

        Blocks until the whole batch has finished. Returns an empty list if
        nothing was pending. Each returned :class:`Job` has been updated in
        place with its final ``status`` and ``result``/``error``, and -- if
        this queue was built with a ``history_recorder`` -- already
        persisted via it.
        """
        with self._lock:
            batch = [job for job in self._jobs.values() if job.status is JobStatus.PENDING]
        if not batch:
            return []

        with ThreadPoolExecutor(max_workers=self._max_concurrency) as executor:
            futures = [executor.submit(self._run_job, job) for job in batch]
            for future in futures:
                future.result()  # re-raise any unexpected executor-level error

        return batch

    def _run_job(self, job: Job) -> None:
        """Execute one job's pipeline run, record its outcome, and persist/notify.

        Runs entirely on the calling (worker) thread. ``self._history_recorder``
        (if configured) is called once as ``job`` transitions to ``RUNNING``
        (COL-108 -- so an in-progress job is already visible in job history,
        not only once it finishes) and again once it reaches its terminal
        status (``SUCCEEDED``/``FAILED``), which is also when
        ``self._record_tracked_media`` (a no-op unless the job actually
        ``SUCCEEDED`` and a ``tracked_media_recorder`` is configured, COL-95)
        and ``self._notify_failure`` (a no-op unless the job actually
        ``FAILED`` and a ``failure_notifier`` is configured) run -- all
        outside ``self._lock``, since by that point only this thread ever
        touches this particular ``job`` (each job is submitted to the
        executor exactly once), so there is nothing left to race against.
        """
        with self._lock:
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(UTC)
        self._record_history(job)
        logger.info(
            "job %s started: file=%s targets=%s",
            job.id,
            job.file_path,
            _enabled_targets_for_log(job.settings),
        )

        try:
            result = self._pipeline_runner(job.file_path, job.settings, **self._pipeline_kwargs)
        except Exception as exc:  # noqa: BLE001 - captured as the job's outcome, not re-raised
            with self._lock:
                job.error = exc
                job.status = JobStatus.FAILED
                job.ended_at = datetime.now(UTC)
            logger.exception("job %s failed with an unexpected error", job.id)
            self._record_history(job)
            self._record_tracked_media(job)
            self._notify_failure(job)
            return

        with self._lock:
            job.result = result
            job.status = JobStatus.SUCCEEDED if result.success else JobStatus.FAILED
            job.ended_at = datetime.now(UTC)
        if job.status is JobStatus.SUCCEEDED:
            logger.info("job %s completed: file=%s -- %s", job.id, job.file_path, result.detail)
        self._record_history(job)
        self._record_tracked_media(job)
        self._notify_failure(job)

    def _record_history(self, job: Job) -> None:
        """Persist ``job``'s current state, if configured to.

        Called at every stage of ``job``'s lifecycle -- ``PENDING`` (from
        :meth:`enqueue`), ``RUNNING``, and its terminal status (from
        :meth:`_run_job`) -- so job history always reflects what ``job`` is
        doing right now, not just its final outcome (COL-108).
        """
        if self._history_recorder is not None:
            self._history_recorder(job)

    def _record_tracked_media(self, job: Job) -> None:
        """Flip ``job``'s processed targets to ``PROCESSED`` in tracked media (COL-95).

        A no-op for a job that didn't reach ``SUCCEEDED`` (there is nothing
        new to record -- a ``FAILED`` job added no tracks, and a later scan
        re-probing the file is what :func:`~collapsarr.media.service.
        upsert_tracked_media` is for), or when no ``tracked_media_recorder``
        was configured.
        """
        if self._tracked_media_recorder is None or job.status is not JobStatus.SUCCEEDED:
            return
        self._tracked_media_recorder(job)

    def _notify_failure(self, job: Job) -> None:
        """Dispatch a failure notification for ``job``, if configured and it failed.

        A no-op for a job that reached ``SUCCEEDED``, or when no
        ``failure_notifier`` was configured. ``self._failure_notifier`` is
        expected to never raise on its own (COL-37's
        :func:`~collapsarr.jobs.failure_notify.notify_job_failure` guarantees
        this), but it is called inside a defensive ``try``/``except`` anyway
        -- a notification problem must never be able to fail the job it is
        reporting on, or the worker thread running it.
        """
        if self._failure_notifier is None or job.status is not JobStatus.FAILED:
            return
        try:
            self._failure_notifier(job)
        except Exception:  # noqa: BLE001 - a notifier failure must never fail the job
            pass
