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
``history_recorder`` is configured (see :class:`JobQueue`), that same
worker thread calls it once the job reaches a terminal status, so every job
run is persisted (COL-21, :mod:`collapsarr.jobs.history`) without the
caller having to remember to do it.

This module deliberately does not import :mod:`collapsarr.jobs.history`
itself (that module imports *this* one, for :class:`Job`/:class:`JobStatus`
-- importing it back here would be circular). Instead ``history_recorder``
is a plain injected callable, the same seam ``pipeline_runner`` already
uses; :func:`collapsarr.jobs.history.make_history_recorder` builds one bound
to a session factory.

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
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from collapsarr.config import Settings, get_settings
from collapsarr.downmix.pipeline import PipelineResult, run_downmix_pipeline
from collapsarr.downmix.targets import DownmixSettings

DEFAULT_MAX_CONCURRENCY = 1

#: Signature every pipeline runner (real or injected-for-tests) must match:
#: ``(file_path, settings, **pipeline_kwargs) -> PipelineResult``.
PipelineRunner = Callable[..., PipelineResult]


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


#: Signature a ``history_recorder`` must match: takes the just-terminated
#: ``Job`` and persists it (see :func:`collapsarr.jobs.history.make_history_recorder`).
HistoryRecorder = Callable[[Job], None]


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

    ``history_recorder``, when set, is called with each :class:`Job` from
    the same worker thread that ran it, right after it reaches a terminal
    status (``SUCCEEDED``/``FAILED``) -- see :func:`collapsarr.jobs.history.
    make_history_recorder` for the constructor that builds one bound to a
    real DB session factory. Since :meth:`run_pending` runs jobs across a
    :class:`~concurrent.futures.ThreadPoolExecutor` (up to ``max_concurrency``
    at once), ``history_recorder`` must itself be safe to call concurrently
    from multiple threads; :func:`~collapsarr.jobs.history.
    make_history_recorder` satisfies this by opening a fresh
    :class:`~sqlalchemy.orm.Session` per call rather than sharing one --
    SQLAlchemy sessions aren't thread-safe, but a ``sessionmaker`` safely
    creates independent sessions from any thread.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        pipeline_runner: PipelineRunner = run_downmix_pipeline,
        pipeline_kwargs: Mapping[str, Any] | None = None,
        history_recorder: HistoryRecorder | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")
        self._max_concurrency = max_concurrency
        self._pipeline_runner = pipeline_runner
        self._pipeline_kwargs = dict(pipeline_kwargs or {})
        self._history_recorder = history_recorder
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
    ) -> JobQueue:
        """Build a :class:`JobQueue` whose concurrency cap comes from Settings.

        ``settings`` defaults to the process-wide cached
        :func:`~collapsarr.config.get_settings`. Its ``job_max_concurrency``
        (default 1, env ``COLLAPSARR_JOB_MAX_CONCURRENCY``) becomes
        ``max_concurrency``.

        Unlike the raw :meth:`__init__` (where ``history_recorder`` defaults
        to ``None`` -- the right default for lightweight unit construction
        that doesn't want DB writes, e.g. COL-20's concurrency tests), this
        factory is the production path: when ``history_recorder`` isn't
        passed explicitly, it defaults to a *real* one -- built via
        :func:`collapsarr.jobs.history.make_history_recorder`, bound to a
        session factory for ``resolved``'s database (schema created via
        :func:`~collapsarr.database.init_db` if not already present) --
        rather than staying ``None``. This mirrors how ``pipeline_runner``
        already defaults to the real :func:`~collapsarr.downmix.pipeline.
        run_downmix_pipeline` in the raw ``__init__``: a bare
        ``JobQueue.from_settings()`` call, with no extra plumbing, persists
        history for real. Pass ``history_recorder`` explicitly (or ``None``
        isn't obtainable here -- construct via :meth:`__init__` directly
        instead) to opt out.

        The database engine backing that default recorder is created once,
        here, for this :class:`JobQueue` instance; it is not shared with
        the FastAPI app's own request-scoped engine (see
        :mod:`collapsarr.main`). For SQLite (this project's only supported
        backend today) that's safe -- both point at the same on-disk file --
        but it does mean calling this factory repeatedly opens a new engine
        each time, so production code should call it once and hold onto the
        resulting :class:`JobQueue` (e.g. on ``app.state``), the same way it
        already holds onto one session factory.

        The import of :mod:`collapsarr.jobs.history` below is deferred
        (inside this method, not at module scope) because that module
        imports *this* one (for :class:`Job`/:class:`JobStatus`) -- the same
        defer-to-break-a-cycle trick :func:`collapsarr.database.init_db`
        already uses for its own model-registration imports.
        """
        resolved = settings or get_settings()

        resolved_history_recorder = history_recorder
        if resolved_history_recorder is None:
            from collapsarr.database import (
                create_engine_from_settings,
                create_session_factory,
                init_db,
            )
            from collapsarr.jobs.history import make_history_recorder

            engine = create_engine_from_settings(resolved)
            init_db(engine)
            resolved_history_recorder = make_history_recorder(create_session_factory(engine))

        return cls(
            max_concurrency=resolved.job_max_concurrency,
            pipeline_runner=pipeline_runner,
            pipeline_kwargs=pipeline_kwargs,
            history_recorder=resolved_history_recorder,
        )

    @property
    def max_concurrency(self) -> int:
        """The configured cap on simultaneously running jobs."""
        return self._max_concurrency

    def enqueue(self, file_path: str | Path, settings: DownmixSettings) -> Job:
        """Add a file + its target/language context to the queue as a new job.

        Returns the created :class:`Job` (status ``PENDING``) immediately;
        it is not run until a subsequent :meth:`run_pending` call.
        """
        job = Job(file_path=Path(file_path), settings=settings)
        with self._lock:
            self._jobs[job.id] = job
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
        """Execute one job's pipeline run, record its outcome, and persist it.

        Runs entirely on the calling (worker) thread. Once ``job`` reaches
        its terminal status (``SUCCEEDED``/``FAILED``), ``self._history_recorder``
        (if configured) is called with it -- outside ``self._lock``, since by
        that point only this thread ever touches this particular ``job``
        (each job is submitted to the executor exactly once), so there is
        nothing left to race against.
        """
        with self._lock:
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(UTC)

        try:
            result = self._pipeline_runner(job.file_path, job.settings, **self._pipeline_kwargs)
        except Exception as exc:  # noqa: BLE001 - captured as the job's outcome, not re-raised
            with self._lock:
                job.error = exc
                job.status = JobStatus.FAILED
                job.ended_at = datetime.now(UTC)
            self._record_history(job)
            return

        with self._lock:
            job.result = result
            job.status = JobStatus.SUCCEEDED if result.success else JobStatus.FAILED
            job.ended_at = datetime.now(UTC)
        self._record_history(job)

    def _record_history(self, job: Job) -> None:
        """Persist ``job``'s just-reached terminal state, if configured to."""
        if self._history_recorder is not None:
            self._history_recorder(job)
