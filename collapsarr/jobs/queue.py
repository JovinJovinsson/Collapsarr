"""Job queue and bounded-concurrency worker pool (COL-20).

Wires the Downmix Engine's end-to-end pipeline
(:func:`~collapsarr.downmix.pipeline.run_downmix_pipeline`, COL-19) to a job
queue: :meth:`JobQueue.enqueue` a file plus its target/language context (a
:class:`~collapsarr.downmix.targets.DownmixSettings`), then
:meth:`JobQueue.run_pending` drains the queue, running at most
``max_concurrency`` jobs at once (default 1).

Each job's execution invokes the pipeline synchronously in a worker thread
and captures whatever it returns (or, as a safety net, whatever it raises)
onto the :class:`Job` itself -- ``status`` plus ``result``/``error`` -- ready
for a future job-history feature (COL-21) to persist.

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

    ``id`` uniquely identifies the job (useful once a job-history feature,
    COL-21, needs a stable key to persist against). ``status`` and
    ``result``/``error`` start empty and are filled in by the queue as the
    job runs -- never mutate them directly.

    ``result`` carries the pipeline's :class:`~collapsarr.downmix.pipeline.PipelineResult`
    when the pipeline ran (success, no-op, or a captured failure at any
    stage). ``error`` is populated instead only in the unexpected case where
    the pipeline runner itself raised rather than returning a result (the
    real pipeline never does this -- see its own docstring -- but an
    injected runner in a test, or a future alternate runner, might).
    """

    file_path: Path
    settings: DownmixSettings
    id: UUID = field(default_factory=uuid4)
    status: JobStatus = JobStatus.PENDING
    result: PipelineResult | None = None
    error: BaseException | None = None


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
    """

    def __init__(
        self,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        pipeline_runner: PipelineRunner = run_downmix_pipeline,
        pipeline_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")
        self._max_concurrency = max_concurrency
        self._pipeline_runner = pipeline_runner
        self._pipeline_kwargs = dict(pipeline_kwargs or {})
        self._lock = threading.Lock()
        self._jobs: dict[UUID, Job] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        pipeline_runner: PipelineRunner = run_downmix_pipeline,
        pipeline_kwargs: Mapping[str, Any] | None = None,
    ) -> JobQueue:
        """Build a :class:`JobQueue` whose concurrency cap comes from Settings.

        ``settings`` defaults to the process-wide cached
        :func:`~collapsarr.config.get_settings`. Its ``job_max_concurrency``
        (default 1, env ``COLLAPSARR_JOB_MAX_CONCURRENCY``) becomes
        ``max_concurrency``.
        """
        resolved = settings or get_settings()
        return cls(
            max_concurrency=resolved.job_max_concurrency,
            pipeline_runner=pipeline_runner,
            pipeline_kwargs=pipeline_kwargs,
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
        place with its final ``status`` and ``result``/``error``.
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
        """Execute one job's pipeline run and record its outcome onto ``job``."""
        with self._lock:
            job.status = JobStatus.RUNNING

        try:
            result = self._pipeline_runner(job.file_path, job.settings, **self._pipeline_kwargs)
        except Exception as exc:  # noqa: BLE001 - captured as the job's outcome, not re-raised
            with self._lock:
                job.error = exc
                job.status = JobStatus.FAILED
            return

        with self._lock:
            job.result = result
            job.status = JobStatus.SUCCEEDED if result.success else JobStatus.FAILED
