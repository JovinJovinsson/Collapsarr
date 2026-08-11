"""Job queue and persistent priority-pull worker pool (COL-20, COL-164).

Wires the Downmix Engine's end-to-end pipeline
(:func:`~collapsarr.downmix.pipeline.run_downmix_pipeline`, COL-19) to a job
queue: :meth:`JobQueue.enqueue` a file plus its target/language context (a
:class:`~collapsarr.downmix.targets.DownmixSettings`) and it runs, on its own,
as soon as one of the pool's ``max_concurrency`` worker threads is free.

**Priority-pull worker pool (COL-164, ADR 0007).** ``JobQueue`` runs a fixed
pool of ``max_concurrency`` worker threads, started once by :meth:`start` and
living for the process lifetime -- not a fresh
:class:`~concurrent.futures.ThreadPoolExecutor` per
drain cycle. Each worker loops forever: it *claims* the lowest-``priority``
still-``PENDING`` :class:`Job` from the shared, lock-protected ``_jobs`` map
(atomically flipping it ``PENDING`` -> ``RUNNING`` so no two workers claim the
same Job), runs it through the pipeline, then loops back for the next one. A
Job enqueued while every worker is busy is picked up the instant one frees --
there is no "batch" whose run order is frozen at submit time. This is what
makes reordering and cancelling not-yet-started work meaningful right up to the
moment a worker claims a Job:

* :meth:`cancel` removes a still-``PENDING`` Job from ``_jobs`` so it never
  runs (returning ``False``, not raising, if a worker already claimed it).
* :meth:`bump_to_front` reassigns a still-``PENDING`` Job's ``priority`` below
  every other pending Job's, so it is claimed next.

Callers that need to block until the queue has drained (tests, orderly
shutdown) use :meth:`wait_idle`; :meth:`shutdown` stops the pool, letting any
in-flight Job finish first. These are internal ``JobQueue``-level Python APIs;
the HTTP/bulk layer that builds on them is a later slice (COL-161).

**Restart-durable rehydration (COL-166).** The in-memory ``_jobs`` map above
starts empty on every process restart, but a ``PENDING`` ``JobHistory`` row
written before the restart survives (it is a durable DB row) with nothing
left to run it -- a "ghost pending row." :func:`~collapsarr.jobs.rehydrate.
rehydrate_pending_jobs` (a separate module -- see the note on this module's
own imports below for why) fixes that at startup: it reads every ``PENDING``
row, ordered by persisted ``priority``, rebuilds each as a live Job with
settings/preference re-derived fresh from the *current*
:class:`~collapsarr.settings.models.GlobalSettings` row (never replayed from
the row's stored ``target``/``language`` summary, which may now be stale),
and pushes the result onto a queue via :meth:`JobQueue.rehydrate` --
preserving each Job's original ``priority`` rather than renumbering it. It
also seeds :attr:`JobQueue._next_priority` first, via :meth:`JobQueue.
seed_next_priority` -- see that method's docstring, and the comment on
``_next_priority``'s own definition below, for why.

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

**Job kinds (COL-155):** a :class:`Job` is either a ``DOWNMIX`` job (the
original kind -- runs :func:`~collapsarr.downmix.pipeline.run_downmix_pipeline`
via ``pipeline_runner``) or a ``SET_DEFAULT_AUDIO`` job (runs the
disposition-only :func:`~collapsarr.downmix.default_audio_pipeline.
run_default_audio_pipeline` via ``default_audio_pipeline_runner``, COL-153's
manual/bulk Default Audio Track fix). Both kinds share this one
:class:`JobQueue` instance -- the same worker pool (so the same
``max_concurrency`` cap applies across both, not per-kind) and the same
in-memory ``_jobs``/persisted job-history table (so
:class:`~collapsarr.jobs.scheduler.JobScheduler`'s per-file dedup guard,
which matches purely on file path, also spans both kinds transparently --
see its module docstring). :meth:`enqueue` creates a ``DOWNMIX`` job (as it
always has); :meth:`enqueue_default_audio` (COL-155) creates a
``SET_DEFAULT_AUDIO`` one. :meth:`_run_job` dispatches to whichever runner
matches ``job.kind``.

Threads, not asyncio: every stage of the downmix pipeline shells out to
``ffprobe``/``ffmpeg`` via blocking :mod:`subprocess` calls, so a small pool
of ``max_concurrency`` worker threads gives genuine bounded parallelism (the
GIL is released for the whole ``subprocess.run`` call) without pulling the
rest of this synchronous codebase onto an event loop.

``max_concurrency`` is a plain constructor argument. It defaults to 1, and
:meth:`JobQueue.from_settings` sources it from the persisted
:class:`~collapsarr.settings.models.GlobalSettings` row's
``concurrency_limit`` field (COL-165, editable from the Settings UI) --
read once, at construction time, so a change made in the UI takes effect
only after a restart (the pool is a fixed-size thread pool for its whole
process lifetime; there is no live resizing, see :meth:`JobQueue.from_settings`).

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
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from collapsarr.config import Settings, get_settings
from collapsarr.downmix.default_audio import DefaultAudioPreference
from collapsarr.downmix.default_audio_pipeline import run_default_audio_pipeline
from collapsarr.downmix.pipeline import PipelineResult, run_downmix_pipeline
from collapsarr.downmix.targets import DownmixSettings

if TYPE_CHECKING:
    from collapsarr.settings.models import GlobalSettings

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENCY = 1

#: Signature the ``DOWNMIX`` pipeline runner (real or injected-for-tests) must
#: match: ``(file_path, settings, **pipeline_kwargs) -> PipelineResult``.
PipelineRunner = Callable[..., PipelineResult]

#: Signature the ``SET_DEFAULT_AUDIO`` pipeline runner (real or
#: injected-for-tests) must match: ``(file_path, preference, **kwargs) ->
#: PipelineResult`` -- matches :func:`~collapsarr.downmix.
#: default_audio_pipeline.run_default_audio_pipeline` (COL-155).
DefaultAudioPipelineRunner = Callable[..., PipelineResult]


def _enabled_targets_for_log(settings: DownmixSettings) -> str:
    """Render ``settings.enabled_targets`` for a log line, in a stable order."""
    return ",".join(sorted(target.value for target in settings.enabled_targets))


class JobStatus(Enum):
    """Lifecycle state of a single :class:`Job`."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobKind(Enum):
    """Which pipeline a :class:`Job` runs (COL-155).

    ``DOWNMIX`` is the original, and remains the default -- every job
    enqueued before this kind existed (and every backfilled job-history row,
    see the COL-155 migration) reads as ``DOWNMIX``. ``SET_DEFAULT_AUDIO`` is
    the manual/bulk Default Audio Track fix (COL-153's disposition-only
    pipeline, :func:`~collapsarr.downmix.default_audio_pipeline.
    run_default_audio_pipeline`).
    """

    DOWNMIX = "downmix"
    SET_DEFAULT_AUDIO = "set_default_audio"


@dataclass(slots=True)
class Job:
    """One enqueued unit of work: a file plus its target/language (or preference) context.

    ``id`` uniquely identifies the job -- COL-21's job-history layer
    (:mod:`collapsarr.jobs.history`) persists against it as ``job_id``.
    ``status``, ``result``/``error``, and ``started_at``/``ended_at`` start
    empty and are filled in by the queue as the job runs -- never mutate
    them directly.

    ``priority`` (COL-163) likewise starts at a placeholder (``0``) and is
    immediately overwritten by :meth:`JobQueue._enqueue` -- a plain,
    lock-guarded counter on the owning :class:`JobQueue` -- with the next
    value in a monotonically increasing per-queue sequence, so it reads as
    "join position": the first job ever enqueued on a queue gets ``0``, the
    next ``1``, and so on, shared across :meth:`JobQueue.enqueue` and
    :meth:`JobQueue.enqueue_default_audio` (both funnel through
    :meth:`_enqueue`) so the two kinds interleave into one ordering rather
    than each keeping its own. The priority-pull worker pool (COL-164) reads
    ``priority`` back to choose what runs next: a free worker always claims the
    lowest-``priority`` still-``PENDING`` Job, so lower means "runs sooner."
    :meth:`JobQueue.bump_to_front` exploits this by dropping a Job's
    ``priority`` below every other pending Job's (which can push it negative --
    ``priority`` is an ordering key, not a count), leaving the monotonic
    ``_next_priority`` counter that hands out join positions untouched.

    ``kind`` (COL-155) selects which pipeline the job runs -- ``DOWNMIX``
    (the default, and only kind before COL-155) uses ``settings``;
    ``SET_DEFAULT_AUDIO`` uses ``preference`` instead. ``settings`` stays a
    required field (rather than becoming ``Optional``) so every pre-existing
    ``DOWNMIX``-only call site keeps constructing a ``Job`` exactly as
    before; a ``SET_DEFAULT_AUDIO`` job still carries one (built by
    :meth:`JobQueue.enqueue_default_audio` with an empty ``enabled_targets``,
    so job-history's target/language columns correctly read as "no downmix
    target" for it -- see :mod:`collapsarr.jobs.history`), it is simply
    unused by :meth:`JobQueue._run_job` for that kind.

    ``result`` carries the pipeline's :class:`~collapsarr.downmix.pipeline.PipelineResult`
    when the pipeline ran (success, no-op, or a captured failure at any
    stage) -- both pipelines return this same type. ``error`` is populated
    instead only in the unexpected case where the pipeline runner itself
    raised rather than returning a result (neither real pipeline does this
    -- see their own docstrings -- but an injected runner in a test, or a
    future alternate runner, might).

    ``started_at``/``ended_at`` are stamped (UTC) by :meth:`JobQueue._run_job`
    when the job transitions to ``RUNNING`` and when it reaches a terminal
    status, respectively -- the start/end timestamps COL-21's job history
    persists. Both stay ``None`` for a job that has never run.
    """

    file_path: Path
    settings: DownmixSettings
    id: UUID = field(default_factory=uuid4)
    kind: JobKind = JobKind.DOWNMIX
    preference: DefaultAudioPreference | None = None
    priority: int = 0
    status: JobStatus = JobStatus.PENDING
    result: PipelineResult | None = None
    error: BaseException | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


def _run_context_for_log(job: Job) -> str:
    """Render the kind-appropriate context for :meth:`JobQueue._run_job`'s start log line.

    A ``DOWNMIX`` job logs its enabled targets (unchanged from before
    COL-155); a ``SET_DEFAULT_AUDIO`` job logs its resolved preference
    instead, since ``job.settings`` carries nothing meaningful for it.
    """
    if job.kind is JobKind.SET_DEFAULT_AUDIO:
        preference = job.preference
        if preference is None:
            return "preference=none"  # defensive; enqueue_default_audio always sets one
        return f"preference={preference.language}/{preference.channel_tier.value}"
    return f"targets={_enabled_targets_for_log(job.settings)}"


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
        queue.start()  # spin up the worker pool
        queue.enqueue("/media/movie.mkv", DownmixSettings())     # runs in the pool
        queue.enqueue("/media/episode.mkv", DownmixSettings())   # runs in the pool
        queue.wait_idle()  # block until both have finished (e.g. in a test)
        queue.shutdown()

    Once :meth:`start` has spun up the pool of ``max_concurrency`` worker
    threads, each :meth:`enqueue`/:meth:`enqueue_default_audio` makes its Job
    immediately eligible to run -- so there is no batch to "drain": a free
    worker claims the lowest-``priority`` pending Job and runs it, in place,
    updating its ``status``/``result``. Before :meth:`start` (or after
    :meth:`shutdown`), enqueue just records a ``PENDING`` Job that waits for
    the pool. Because work runs asynchronously, callers that must observe
    completion (tests, orderly shutdown) call :meth:`wait_idle` to block until
    nothing is ``PENDING`` or ``RUNNING``; :meth:`shutdown` stops the pool,
    letting any in-flight Job finish first. :class:`JobQueue` is also a context
    manager, shutting the pool down on exit.

    ``history_recorder``, when set, is called with each :class:`Job` three
    times over its lifecycle: immediately on :meth:`enqueue` (``PENDING``,
    from whichever thread called ``enqueue``), then from the worker thread
    that runs it as it transitions to ``RUNNING``, and again right after it
    reaches a terminal status (``SUCCEEDED``/``FAILED``) -- so a job is
    visible in job history the instant it's queued, not only once it
    finishes (COL-108). See :func:`collapsarr.jobs.history.
    make_history_recorder` for the constructor that builds one bound to a
    real DB session factory. Since the worker pool runs jobs on up to
    ``max_concurrency`` threads at once, ``history_recorder`` must itself be
    safe to call concurrently from multiple threads; :func:`~collapsarr.jobs.history.
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

    ``default_audio_pipeline_runner`` (COL-155) is the second
    constructor-injected runner: it runs a ``SET_DEFAULT_AUDIO`` job's
    disposition-only fix (:func:`~collapsarr.downmix.default_audio_pipeline.
    run_default_audio_pipeline`) the same way ``pipeline_runner`` runs a
    ``DOWNMIX`` job's pipeline. Both kinds share every other seam on this
    class -- ``max_concurrency``, ``history_recorder``, ``failure_notifier``,
    ``tracked_media_recorder`` -- so a ``SET_DEFAULT_AUDIO`` job is visible
    in job history, dispatches a failure notification, and is bounded by the
    same concurrency cap exactly like a ``DOWNMIX`` job. It is not, however,
    a source of *new* tracked-media targets (it never adds a track), so
    :meth:`_record_tracked_media` is a no-op for it in practice (its
    :attr:`~collapsarr.downmix.pipeline.PipelineResult.tracks_added` is
    always empty).
    """

    def __init__(
        self,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        pipeline_runner: PipelineRunner = run_downmix_pipeline,
        pipeline_kwargs: Mapping[str, Any] | None = None,
        default_audio_pipeline_runner: DefaultAudioPipelineRunner = run_default_audio_pipeline,
        history_recorder: HistoryRecorder | None = None,
        failure_notifier: FailureNotifier | None = None,
        tracked_media_recorder: TrackedMediaRecorder | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")
        self._max_concurrency = max_concurrency
        self._pipeline_runner = pipeline_runner
        self._pipeline_kwargs = dict(pipeline_kwargs or {})
        self._default_audio_pipeline_runner = default_audio_pipeline_runner
        self._history_recorder = history_recorder
        self._failure_notifier = failure_notifier
        self._tracked_media_recorder = tracked_media_recorder
        self._lock = threading.Lock()
        #: Guards every access to ``_jobs``/``_next_priority``/``_shutdown``
        #: and coordinates the worker pool. Workers ``wait`` on it for a
        #: claimable Job; :meth:`_enqueue`/:meth:`bump_to_front`/:meth:`shutdown`
        #: ``notify_all`` it. :meth:`wait_idle` waits on it for the queue to
        #: drain. Wraps ``_lock``, so every ``with self._lock:`` block below is
        #: equally a critical section of this condition.
        self._cond = threading.Condition(self._lock)
        self._jobs: dict[UUID, Job] = {}
        #: Next value :meth:`_enqueue` will hand out as a job's ``priority``
        #: (COL-163) -- a plain lock-guarded counter, starting at 0 and
        #: incrementing once per enqueued job (across both ``enqueue`` and
        #: ``enqueue_default_audio``), so "priority" reads as join order:
        #: lower means enqueued earlier. Only ever moves forward;
        #: :meth:`bump_to_front` reorders by lowering a Job's own ``priority``,
        #: never by rewinding this counter. This is *process-local* only: it
        #: resets to 0 on construction, so on a fresh restart it would hand out
        #: values that collide with rows already on disk unless seeded first.
        #: :meth:`seed_next_priority` (COL-166) is how a caller doing restart
        #: rehydration fixes that -- called with ``max(persisted priority) + 1``
        #: across *every* ``JobHistory`` row (not just the ``PENDING`` ones
        #: being rehydrated, since a completed/failed row may carry a higher
        #: priority than any pending one) before any post-restart job is
        #: enqueued, so freshly-submitted work always sorts *after* every
        #: rehydrated job rather than wrongly jumping the queue.
        self._next_priority = 0
        #: The persistent worker pool (COL-164). Started explicitly by a
        #: :meth:`start` call (never implicitly on enqueue), then lives until
        #: :meth:`shutdown` (or process exit -- the threads are daemons).
        #: :meth:`_enqueue` does *not* start it; it only ``notify_all``s to
        #: wake any workers already waiting for a claimable Job.
        self._workers: list[threading.Thread] = []
        self._started = False
        #: Set by :meth:`shutdown`; tells every idle worker to exit its loop.
        self._shutdown = False
        #: Count of jobs a worker has claimed but not yet *fully* finished --
        #: including the post-run history/tracked-media/failure side effects,
        #: not just the pipeline call. :meth:`wait_idle` blocks while this is
        #: non-zero (or anything is still ``PENDING``), so it doesn't return
        #: until every side effect of every job has completed -- the guarantee
        #: the old ``run_pending`` gave by joining its futures.
        self._active = 0

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        pipeline_runner: PipelineRunner = run_downmix_pipeline,
        pipeline_kwargs: Mapping[str, Any] | None = None,
        default_audio_pipeline_runner: DefaultAudioPipelineRunner = run_default_audio_pipeline,
        history_recorder: HistoryRecorder | None = None,
        failure_notifier: FailureNotifier | None = None,
        tracked_media_recorder: TrackedMediaRecorder | None = None,
    ) -> JobQueue:
        """Build a :class:`JobQueue` whose concurrency cap comes from persisted Settings.

        ``settings`` defaults to the process-wide cached
        :func:`~collapsarr.config.get_settings`, used here only to resolve the
        database to read from. ``max_concurrency`` itself comes from the
        persisted :class:`~collapsarr.settings.models.GlobalSettings` row's
        ``concurrency_limit`` (default 1, editable from the Settings UI,
        COL-165) -- read once here, at construction time, the same as every
        other field this factory reads off that row. Changing it in the
        Settings UI does not resize an already-running pool: the new value
        only takes effect the next time the process (and so this factory)
        starts, since the worker pool is a fixed-size thread pool for its
        whole process lifetime (live resizing is out of scope).

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

        ``default_audio_pipeline_runner`` (COL-155) defaults to the real
        :func:`~collapsarr.downmix.default_audio_pipeline.
        run_default_audio_pipeline`, mirroring how ``pipeline_runner``
        already defaults to the real downmix pipeline -- unlike the
        automatic in-band fix above, the manual/bulk ``SET_DEFAULT_AUDIO``
        preference isn't read from Settings here: :meth:`JobScheduler.
        trigger_set_default_audio` resolves it live, per call, and passes it
        straight to :meth:`enqueue_default_audio`.

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
        from collapsarr.settings.service import get_global_settings

        upgrade_to_head(resolved)
        engine = create_engine_from_settings(resolved)
        session_factory = create_session_factory(engine)

        # Read once here, at construction time: concurrency_limit (COL-165)
        # sizes the pool below, and auto_set_default_audio (COL-152) feeds
        # _resolve_pipeline_kwargs -- both come off this same singleton row,
        # so one read serves both rather than opening a second session.
        with session_factory() as session:
            global_settings = get_global_settings(session)

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
            max_concurrency=global_settings.concurrency_limit,
            pipeline_runner=pipeline_runner,
            pipeline_kwargs=cls._resolve_pipeline_kwargs(pipeline_kwargs, global_settings),
            default_audio_pipeline_runner=default_audio_pipeline_runner,
            history_recorder=resolved_history_recorder,
            failure_notifier=resolved_failure_notifier,
            tracked_media_recorder=resolved_tracked_media_recorder,
        )

    @staticmethod
    def _resolve_pipeline_kwargs(
        pipeline_kwargs: Mapping[str, Any] | None,
        global_settings: GlobalSettings,
    ) -> dict[str, Any]:
        """Fold the persisted Default Audio Track preference into ``pipeline_kwargs`` (COL-152).

        Takes ``global_settings`` -- the same singleton
        :class:`~collapsarr.settings.models.GlobalSettings` row
        :meth:`from_settings` already reads once for ``concurrency_limit``
        (COL-165), reused here rather than opening a second session -- and,
        **only** when its opt-in ``auto_set_default_audio`` toggle is on,
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

        The import below is deferred, matching the surrounding factory: the
        settings service pulls in the ORM/adapters, which don't need to load for
        a lightweight :meth:`__init__` construction that never touches Settings.
        """
        from collapsarr.settings.service import as_default_audio_preference

        resolved = dict(pipeline_kwargs or {})
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
        """Add a file + its target/language context to the queue as a ``DOWNMIX`` job.

        Returns the created :class:`Job` (status ``PENDING``) immediately; a
        free worker in the running pool then claims and runs it (or, if
        :meth:`start` hasn't been called yet, it waits ``PENDING`` until the
        pool starts). Persisted via ``history_recorder`` (if configured) right
        away, so a job shows up in job history -- and so the Activity view --
        the instant it's queued, rather than only once it finishes (COL-108).
        """
        job = Job(file_path=Path(file_path), settings=settings, kind=JobKind.DOWNMIX)
        return self._enqueue(job)

    def enqueue_default_audio(
        self, file_path: str | Path, preference: DefaultAudioPreference
    ) -> Job:
        """Add a file + its Default Audio Track preference as a ``SET_DEFAULT_AUDIO`` job (COL-155).

        Mirrors :meth:`enqueue` exactly, for the disposition-only fix: same
        immediate ``PENDING`` job, same worker pool runs it, same
        ``history_recorder`` visibility. The job's ``settings`` is a
        placeholder :class:`~collapsarr.downmix.targets.DownmixSettings`
        with an empty ``enabled_targets`` -- unused by :meth:`_run_job` for
        this kind, but keeping it well-formed means job-history's
        target/language columns correctly read as "no downmix target" for
        this job rather than the ``DownmixSettings`` default (Stereo).

        Shares this queue's ``_jobs`` dict (and so its ``max_concurrency``
        cap and, via :class:`~collapsarr.jobs.scheduler.JobScheduler`'s
        file-path-only dedup guard, its de-duplication) with every
        ``DOWNMIX`` job already on it -- the two kinds are not run through
        separate pools.
        """
        job = Job(
            file_path=Path(file_path),
            settings=DownmixSettings(enabled_targets=frozenset()),
            kind=JobKind.SET_DEFAULT_AUDIO,
            preference=preference,
        )
        return self._enqueue(job)

    def _enqueue(self, job: Job) -> Job:
        """Shared tail of :meth:`enqueue`/:meth:`enqueue_default_audio`: assign priority + persist.

        ``job.priority`` (COL-163) is assigned here, under ``self._lock``,
        atomically with the job's insertion into ``self._jobs`` -- so
        priority order and ``self._jobs`` insertion order (what
        :meth:`list_jobs` returns) always agree, and two jobs enqueued
        concurrently from different threads never race to the same
        priority value.

        Wakes a worker (via ``notify_all``) so a running pool claims the new
        Job as soon as one is free. If :meth:`start` hasn't been called yet the
        notify is harmless (no workers are waiting) and the Job simply sits
        ``PENDING`` until the pool starts -- so ``enqueue`` deterministically
        returns a not-yet-run Job, and the pool's lifecycle stays explicit
        (:meth:`start`/:meth:`shutdown`), owned by whoever built the queue.
        """
        with self._cond:
            job.priority = self._next_priority
            self._next_priority += 1
            self._jobs[job.id] = job
            self._cond.notify_all()
        self._record_history(job)
        return job

    def start(self) -> None:
        """Start the persistent worker pool (COL-164). Idempotent.

        Spawns ``max_concurrency`` daemon worker threads that live until
        :meth:`shutdown` (or process exit). Safe to call before or after work
        is enqueued -- workers pick up whatever is already ``PENDING`` on their
        first pass. A second call, or a call after :meth:`shutdown`, is a
        no-op (the pool is never resurrected).
        """
        with self._cond:
            if self._started or self._shutdown:
                return
            self._workers = [
                threading.Thread(
                    target=self._worker_loop,
                    name=f"collapsarr-jobworker-{index}",
                    daemon=True,
                )
                for index in range(self._max_concurrency)
            ]
            self._started = True
            for worker in self._workers:
                worker.start()

    def get_job(self, job_id: UUID) -> Job | None:
        """Return the job with ``job_id``, or ``None`` if no such job exists."""
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        """Return every job ever enqueued on this queue, in enqueue order."""
        with self._lock:
            return list(self._jobs.values())

    def cancel(self, job_id: UUID) -> bool:
        """Remove a still-``PENDING`` job so it never runs (COL-164).

        Returns ``True`` if the job was pending and has now been removed --
        it will not run, and no longer appears in :meth:`list_jobs`. Returns
        ``False`` (not an error) if the job is unknown or a worker has already
        claimed it (its status is no longer ``PENDING``): that's just "too
        late," and the already-claimed job runs to completion normally.

        Does **not** delete the job's ``JobHistory`` row (its ``PENDING`` row,
        written at enqueue, stays as-is): row cleanup is a later slice's
        concern, not this primitive's.
        """
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None or job.status is not JobStatus.PENDING:
                return False
            del self._jobs[job_id]
            self._cond.notify_all()  # a waiter in wait_idle may now be idle
            return True

    def bump_to_front(self, job_id: UUID) -> bool:
        """Make a still-``PENDING`` job the next one a free worker claims (COL-164).

        Reassigns the job's ``priority`` to one below the current minimum
        pending priority, so it sorts ahead of every other pending job. Returns
        ``True`` on success, ``False`` (not an error) if the job is unknown or
        already claimed/terminal -- the same "too late" contract as
        :meth:`cancel`.

        The new value can be negative; ``priority`` is only ever compared, so
        that's fine, and the monotonic ``_next_priority`` counter is left
        alone -- future enqueues keep getting fresh, ever-increasing join
        positions that never collide with a bumped job's.
        """
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None or job.status is not JobStatus.PENDING:
                return False
            min_pending = min(
                pending.priority
                for pending in self._jobs.values()
                if pending.status is JobStatus.PENDING
            )
            job.priority = min_pending - 1
            self._cond.notify_all()
            return True

    def seed_next_priority(self, min_value: int) -> None:
        """Raise :attr:`_next_priority` to at least ``min_value`` (COL-166).

        Called once, at process startup, by restart rehydration
        (:func:`~collapsarr.jobs.rehydrate.rehydrate_pending_jobs`) with
        ``max(persisted priority) + 1`` across *every* persisted
        ``JobHistory`` row -- not only the still-``PENDING`` ones being
        rehydrated onto this queue, since a completed/failed row can carry a
        higher ``priority`` than any pending one, and this counter must clear
        every value already written to disk, not just the ones coming back as
        live Jobs. Never lowers the counter -- a no-op if ``min_value`` is not
        greater than the current value, so calling this on an already-used
        queue (or with a stale/smaller value) can't rewind priorities that
        were already handed out.
        """
        with self._lock:
            if min_value > self._next_priority:
                self._next_priority = min_value

    def rehydrate(self, jobs: Iterable[Job]) -> None:
        """Insert already-constructed, still-``PENDING`` Jobs directly into the queue (COL-166).

        Unlike :meth:`enqueue`/:meth:`enqueue_default_audio` (which funnel
        through :meth:`_enqueue` to assign a fresh join-order ``priority``),
        this trusts each ``job.priority`` as given -- restart rehydration
        (:func:`~collapsarr.jobs.rehydrate.rehydrate_pending_jobs`) builds
        ``jobs`` from persisted ``JobHistory`` rows and needs their *original*
        priority order preserved relative to each other, not renumbered as if
        they were just enqueued. Callers must seed :attr:`_next_priority`
        past every persisted priority first (:meth:`seed_next_priority`) so a
        subsequent fresh :meth:`enqueue` never collides with -- or wrongly
        sorts ahead of -- a rehydrated Job's priority.

        Does not call ``history_recorder``: every rehydrated Job already has a
        matching ``PENDING`` ``JobHistory`` row (that's precisely where it was
        read from), so there is nothing new to persist here -- the row is
        brought up to date automatically the next time this Job actually runs
        (:meth:`_run_job` records it again on the ``RUNNING`` transition and
        again on completion, each time recomputing ``target``/``language``
        from the Job's live ``settings``/``preference``).

        Wakes any worker already waiting on a claimable Job (harmless, and a
        no-op, if the pool hasn't been started yet -- the ordinary case, since
        rehydration runs before :meth:`start` at process startup).
        """
        with self._cond:
            for job in jobs:
                self._jobs[job.id] = job
            self._cond.notify_all()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until no job is ``PENDING`` or ``RUNNING`` (COL-164).

        The replacement for the old ``run_pending`` "blocks until the batch
        finished" guarantee, decoupled from submission: it simply waits for
        the whole queue to drain. Returns ``True`` once idle, or ``False`` if
        ``timeout`` (seconds) elapsed first. With no ``timeout`` it waits
        indefinitely. Returns immediately when the queue is already idle
        (including a never-used queue whose pool never started).
        """
        with self._cond:
            if timeout is None:
                while self._has_active_work_locked():
                    self._cond.wait()
                return True
            deadline = time.monotonic() + timeout
            while self._has_active_work_locked():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return not self._has_active_work_locked()
                self._cond.wait(remaining)
            return True

    def _has_active_work_locked(self) -> bool:
        """Whether work is still in flight (call with the lock held).

        True while any job a worker has claimed is still being processed
        (``_active``), or any job is still ``PENDING`` and waiting to be
        claimed.
        """
        if self._active > 0:
            return True
        return any(job.status is JobStatus.PENDING for job in self._jobs.values())

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """Stop the worker pool; any job a worker already claimed still finishes.

        Idempotent. Signals every worker to exit once it finishes whatever it
        is currently running (there is no interruption of an in-flight
        ``ffmpeg`` -- ADR 0007) and, when ``wait`` (the default), joins the
        worker threads before returning. After shutdown the queue accepts no
        new work: a later :meth:`enqueue` records the job but never starts a
        pool to run it.
        """
        with self._cond:
            if self._shutdown:
                return
            self._shutdown = True
            self._cond.notify_all()
            workers = list(self._workers)
        if wait:
            for worker in workers:
                worker.join(timeout=timeout)

    def __enter__(self) -> JobQueue:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    def _worker_loop(self) -> None:
        """One pool worker: claim the next job, run it, repeat until shutdown (COL-164)."""
        while True:
            job = self._claim_next()
            if job is None:  # shutdown signalled while idle
                return
            self._run_job(job)

    def _claim_next(self) -> Job | None:
        """Block until a job is claimable, atomically claim it, and return it.

        Returns the lowest-``priority`` still-``PENDING`` job, flipped to
        ``RUNNING`` under the lock so no other worker can claim it and a
        concurrent :meth:`cancel` correctly loses the race (sees it already
        non-``PENDING``). Returns ``None`` only when :meth:`shutdown` was
        signalled while this worker was idle -- the worker's cue to exit.
        """
        with self._cond:
            while True:
                if self._shutdown:
                    return None
                job = self._lowest_priority_pending_locked()
                if job is not None:
                    job.status = JobStatus.RUNNING
                    job.started_at = datetime.now(UTC)
                    self._active += 1  # stays counted until _run_job fully finishes
                    return job
                self._cond.wait()

    def _lowest_priority_pending_locked(self) -> Job | None:
        """The pending job a free worker should claim next (call with the lock held).

        A linear scan for the minimum ``priority`` among ``PENDING`` jobs --
        the queue holds media-library-scale job counts, so an O(n) pick per
        claim is simpler and less error-prone than a heap that would also have
        to support :meth:`cancel`'s arbitrary removal and
        :meth:`bump_to_front`'s key decrease.
        """
        pending = [job for job in self._jobs.values() if job.status is JobStatus.PENDING]
        if not pending:
            return None
        return min(pending, key=lambda job: job.priority)

    def _run_job(self, job: Job) -> None:
        """Execute one already-claimed job's pipeline run, record its outcome, and persist/notify.

        Runs entirely on the calling (worker) thread. ``job`` arrives already
        claimed by :meth:`_claim_next` (status ``RUNNING``, ``started_at``
        stamped, under the lock). ``self._history_recorder`` (if configured)
        is called once here for that ``RUNNING`` transition (COL-108 -- so an
        in-progress job is already visible in job history, not only once it
        finishes) and again once it reaches its terminal status
        (``SUCCEEDED``/``FAILED``), which is also when
        ``self._record_tracked_media`` (a no-op unless the job actually
        ``SUCCEEDED`` and a ``tracked_media_recorder`` is configured, COL-95)
        and ``self._notify_failure`` (a no-op unless the job actually
        ``FAILED`` and a ``failure_notifier`` is configured) run -- all
        outside ``self._lock``, since by that point only this thread ever
        touches this particular ``job`` (each job is claimed by exactly one
        worker), so there is nothing left to race against.

        Dispatches on ``job.kind`` (COL-155) for which runner actually
        executes the pipeline: ``DOWNMIX`` calls ``self._pipeline_runner``
        with ``job.settings``, ``SET_DEFAULT_AUDIO`` calls
        ``self._default_audio_pipeline_runner`` with ``job.preference``
        instead -- everything else below (history/tracked-media/failure
        handling) is identical for both kinds.
        """
        try:
            self._record_history(job)
            logger.info(
                "job %s started: kind=%s file=%s %s",
                job.id,
                job.kind.value,
                job.file_path,
                _run_context_for_log(job),
            )

            try:
                if job.kind is JobKind.SET_DEFAULT_AUDIO:
                    assert job.preference is not None  # enqueue_default_audio always sets this
                    result = self._default_audio_pipeline_runner(job.file_path, job.preference)
                else:
                    result = self._pipeline_runner(
                        job.file_path, job.settings, **self._pipeline_kwargs
                    )
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
        finally:
            # Only now -- after every side effect -- is the job fully done, so
            # this is where wait_idle is allowed to observe it as no longer
            # active. The ``finally`` guarantees the count is released (and
            # waiters woken) even if a recorder/notifier raised unexpectedly,
            # so a stray side-effect error can never wedge wait_idle/shutdown.
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

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
