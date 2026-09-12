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
  A Job a worker has *already* claimed is instead hard-killed by
  :meth:`cancel_running` (COL-192), which terminates its in-flight
  ffmpeg/ffprobe subprocess so the Job fails out and frees its slot at once --
  the manual mid-flight cancellation that supersedes ADR 0007's original
  "no interruption of an in-flight ffmpeg."
* :meth:`bump_to_front` reassigns a still-``PENDING`` Job's ``priority`` below
  every other pending Job's, so it is claimed next.
* :meth:`force_start` (COL-229, "Process Now") claims a still-``PENDING`` Job
  and runs it *immediately*, on a dedicated thread outside the fixed pool --
  unlike :meth:`bump_to_front`, which only reorders within the existing
  pending queue (still subject to Auto-Processing Pause and the Concurrency
  Limit once claimed), this bypasses both.

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
immediately rather than only after the next scan re-probes the file. When a
``plex_analyzer`` is configured, that same worker thread calls it too -- but
only for a job that reached ``SUCCEEDED`` -- so Plex is told to re-analyze
the file's stream-level metadata right after a successful downmix or
Default Audio Track fix rewrites it (COL-211,
:mod:`collapsarr.jobs.plex_analyze`), without waiting for Plex's own library
scan to notice. Finally, when a job-terminal hook is set
(:meth:`JobQueue.set_job_terminal_hook`), that
same worker thread calls it too -- for *every* terminal job, success or
failure alike, unconditionally. :class:`~collapsarr.jobs.scheduler.
JobScheduler` wires its own :meth:`~collapsarr.jobs.scheduler.JobScheduler.
top_up` here (COL-171), so a Job finishing anywhere immediately re-tops-up
the Auto-Queue Limit's budget if a slot just freed -- see that method's
docstring for the full algorithm and a re-entrancy/deadlock analysis of
calling back into this queue from its own worker thread.

This module deliberately does not import :mod:`collapsarr.jobs.history`,
:mod:`collapsarr.jobs.failure_notify`, :mod:`collapsarr.jobs.tracked_media`,
or :mod:`collapsarr.jobs.plex_analyze` itself (those modules import *this*
one, for :class:`Job`/:class:`JobStatus` -- importing them back here would be
circular). Instead ``history_recorder``/``failure_notifier``/
``tracked_media_recorder``/``plex_analyzer`` are plain injected callables,
the same seam ``pipeline_runner`` already uses;
:func:`collapsarr.jobs.history.make_history_recorder`,
:func:`collapsarr.jobs.failure_notify.make_failure_notifier`,
:func:`collapsarr.jobs.tracked_media.make_tracked_media_recorder`, and
:func:`collapsarr.jobs.plex_analyze.make_plex_analyzer` build ones bound to a
session factory.

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
matches ``job.kind`` -- for ``SET_DEFAULT_AUDIO`` that is always exactly one
call to ``default_audio_pipeline_runner``, regardless of what it does
internally: :meth:`from_settings` wires that runner, by default, to
:func:`~collapsarr.jobs.default_audio_dispatch.make_default_audio_pipeline_runner`'s
mechanism-selection gate (COL-247), which itself picks exactly one of the
ffmpeg remux fallback or a direct Plex API write per Job based on whether a
Plex Connection is configured -- never both. This queue has no knowledge of
that choice; it only ever sees one runner and one result.

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
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from collapsarr.config import Settings, get_settings
from collapsarr.downmix.cancellation import CancellationHandle
from collapsarr.downmix.default_audio import DefaultAudioPreference
from collapsarr.downmix.default_audio_pipeline import run_default_audio_pipeline
from collapsarr.downmix.pipeline import PipelineResult, run_downmix_pipeline
from collapsarr.downmix.targets import DownmixSettings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from collapsarr.settings.models import GlobalSettings

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENCY = 1


def _utcnow() -> datetime:
    """Real UTC-now -- :class:`JobQueue`'s default injectable clock (COL-242).

    Mirrors :func:`collapsarr.jobs.scheduler._utcnow` exactly (same signature,
    same body): a plain zero-arg callable a test can swap out via ``JobQueue``'s
    ``now`` constructor parameter, the same seam :class:`~collapsarr.jobs.
    scheduler.JobScheduler` already exposes for its own ``now``.
    """
    return datetime.now(UTC)


_PAUSE_POLL_INTERVAL_SECONDS = 1.0
"""How often a worker blocked purely because :data:`AutoProcessingPauseCheck`
currently returns ``True`` re-checks it (COL-226). Toggling the persisted
``auto_processing_paused`` setting off doesn't otherwise wake an idle
worker -- only :meth:`JobQueue._enqueue`/:meth:`JobQueue.bump_to_front`/
:meth:`JobQueue.shutdown` call ``notify_all`` on the condition a blocked
worker waits on, and none of those fire from a ``PUT /api/settings`` write --
so a paused worker instead polls at this cadence, the same "read fresh,
bounded latency" shape :class:`~collapsarr.jobs.scheduler.JobScheduler`'s
``recently_processed_window_minutes`` dedup cooldown already uses for a live
Settings read. 1 second keeps the worst-case "un-pause to first claim"
latency low without spinning the CPU or hammering the database."""

_SCHEDULE_POLL_INTERVAL_SECONDS = 1.0
"""How often a worker blocked purely because every still-``PENDING`` Job's
``scheduled_at`` (COL-242) is in the future re-checks whether one has come
due. Mirrors :data:`_PAUSE_POLL_INTERVAL_SECONDS`'s reasoning exactly: nothing
calls ``notify_all`` on the condition a blocked worker waits on merely because
wall-clock time passed, so a worker that would otherwise wait indefinitely
(the ordinary "nothing claimable, not paused" case) instead polls at this
cadence whenever the *reason* nothing is claimable is a future
``scheduled_at`` rather than a genuinely empty/paused queue -- see
:meth:`JobQueue._has_future_scheduled_pending_locked`. In production this
fires for a downmix-triggered ``SET_DEFAULT_AUDIO`` Job (COL-251, via
:meth:`JobQueue._schedule_default_audio_job`) while it waits out its
``default_audio_delay_minutes`` due time; every other Job kind/trigger still
leaves ``scheduled_at`` unset and so is never affected by this poll at all."""

#: Signature the ``DOWNMIX`` pipeline runner (real or injected-for-tests) must
#: match: ``(file_path, settings, **pipeline_kwargs) -> PipelineResult``.
PipelineRunner = Callable[..., PipelineResult]

#: Signature the ``SET_DEFAULT_AUDIO`` pipeline runner (real or
#: injected-for-tests) must match: ``(file_path, preference, **kwargs) ->
#: PipelineResult`` -- matches :func:`~collapsarr.downmix.
#: default_audio_pipeline.run_default_audio_pipeline` (COL-155).
DefaultAudioPipelineRunner = Callable[..., PipelineResult]

#: The subset of ``_pipeline_kwargs`` keys (COL-218) that
#: :func:`~collapsarr.downmix.default_audio_pipeline.run_default_audio_pipeline`
#: also accepts, so :meth:`JobQueue._run_job` can forward them to a
#: ``SET_DEFAULT_AUDIO`` job's runner too. **Not** every key: ``pipeline_kwargs``
#: is a ``DOWNMIX``-job-only dict (e.g. it carried
#: ``auto_set_default_audio``/``default_audio_preference`` before COL-251
#: replaced that in-band fold with :meth:`JobQueue._schedule_default_audio_job`
#: below), and :func:`run_default_audio_pipeline` has no catch-all ``**kwargs``
#: for arbitrary ``DOWNMIX``-only keys, so passing the whole dict through
#: unfiltered would raise ``TypeError``. ``ffmpeg_path`` is the only key both
#: pipelines share today. This is unrelated to ``expected_stream_count``
#: (COL-251), which is per-*Job* (``job.expected_stream_count``), not part of
#: this constructor-level ``pipeline_kwargs`` dict -- see
#: :meth:`JobQueue._run_job`'s ``SET_DEFAULT_AUDIO`` branch, which passes it
#: as its own explicit keyword alongside ``cancel_handle``.
_SHARED_DEFAULT_AUDIO_PIPELINE_KWARGS = frozenset({"ffmpeg_path"})

#: Signature the Auto-Processing Pause gate (COL-226, "Auto-Processing
#: Pause") must match: a zero-arg callable returning whether pending-job
#: claiming should currently be paused. Called from
#: :meth:`JobQueue._claim_next` on every claim attempt -- unlike
#: ``max_concurrency`` (read once at construction), a change takes effect on
#: the very next claim, live, with no restart. Deliberately distinct from
#: :class:`~collapsarr.jobs.scheduler.JobScheduler`'s own ``auto_queue_paused``
#: gate (COL-174, "Auto-Queuing Pause"), which only stops the scanner's
#: enqueue/top-up funnel from adding *new* ``PENDING`` Jobs: this callable
#: instead gates the queue's sole pending -> running chokepoint, so it also
#: halts a Job that is already sitting ``PENDING`` (enqueued before the
#: pause, or added to it while paused, e.g. by a manual trigger) from ever
#: starting, while a Job a worker has already claimed keeps running
#: uninterrupted to completion. ``None`` (the default, used by the raw
#: ``__init__`` and every existing test) means "never paused" --
#: :meth:`JobQueue.from_settings` wires a real one reading
#: ``GlobalSettings.auto_processing_paused`` live (see
#: :func:`_make_live_pause_check`).
AutoProcessingPauseCheck = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class DefaultAudioAutoSchedule:
    """Config for auto-scheduling a delayed Default Audio Track Job after a downmix (COL-251).

    Replaces COL-152's in-band fold (``run_downmix_pipeline``'s
    ``auto_set_default_audio``/``default_audio_preference`` parameters, still
    present on that function for direct/test callers, but no longer wired
    into it by :meth:`JobQueue.from_settings`): rather than folding the
    disposition change into the *same* remux, a downmix that adds a new
    stream now enqueues a *separate*, delayed ``SET_DEFAULT_AUDIO`` Job --
    see :meth:`JobQueue._schedule_default_audio_job`. Doing it this way lets
    the Job apply the fix through the COL-247 mechanism-selection gate (a
    verified direct Plex API write when Plex is configured, so the decision
    is based on what Plex itself reports rather than local ffprobe output),
    with ``delay`` giving Plex's own asynchronous ingestion of the new stream
    time to catch up before the write is attempted.

    Built by :meth:`JobQueue.from_settings` from
    ``GlobalSettings.auto_set_default_audio``/``default_audio_language``/
    ``default_audio_channel_tier``/``default_audio_delay_minutes`` -- only
    when the opt-in toggle is on *and* a complete ``(language, channel tier)``
    preference is configured (mirroring :func:`~collapsarr.settings.service.
    as_default_audio_preference`'s own "no actionable preference" contract).
    ``None`` (the :meth:`JobQueue.__init__` default) means the feature is
    off: a downmix Job's success schedules nothing, matching pre-COL-251
    behaviour for every install that hasn't opted in.
    """

    preference: DefaultAudioPreference
    delay: timedelta


def _make_live_pause_check(
    session_factory: sessionmaker[Session],
) -> AutoProcessingPauseCheck:
    """Build a real :data:`AutoProcessingPauseCheck` bound to ``session_factory`` (COL-226).

    Opens a short-lived :class:`~sqlalchemy.orm.Session` per call -- the same
    per-call-session pattern :func:`collapsarr.jobs.history.
    make_history_recorder` already uses -- so a worker calling this from
    :meth:`JobQueue._claim_next` (up to ``max_concurrency`` threads may call
    it concurrently) always reads the *current* persisted
    ``GlobalSettings.auto_processing_paused`` value, never one captured once
    at :meth:`JobQueue.from_settings` construction time.

    The import is deferred, matching every other Settings-service import in
    this module: the settings service pulls in the ORM/adapters, which don't
    need to load for a lightweight ``__init__`` construction that never
    touches Settings.
    """
    from collapsarr.settings.service import get_global_settings

    def _pause_check() -> bool:
        with session_factory() as session:
            return get_global_settings(session).auto_processing_paused

    return _pause_check


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

    ``cancellation`` (COL-192) is the job's hard-kill handle. It stays ``None``
    while the job is ``PENDING`` and is set by :meth:`JobQueue._claim_next` the
    instant a worker flips the job to ``RUNNING`` -- so
    :meth:`JobQueue.cancel_running` can reach the live ffmpeg/ffprobe
    subprocess and terminate it on an explicit user cancel. Not mutated
    directly by anyone else; ``None`` again reads simply as "never ran / not
    running."

    ``scheduled_at`` (COL-242) is an optional due-time gate on an otherwise
    ordinary ``PENDING`` job: ``None`` (the default -- every immediate
    trigger: manual/bulk, the plain webhook, and every ``DOWNMIX`` job)
    means "claimable as soon as a worker is free", exactly the behaviour
    before this field existed. A non-``None`` value means a worker's
    ordinary claim path (:meth:`JobQueue._claim_next`) skips this Job until
    ``scheduled_at`` has passed (compared against the queue's injectable
    :attr:`JobQueue._now` clock) -- it still reads as ``status is
    JobStatus.PENDING`` the whole time (there is no new status), it simply
    isn't *claimable* yet. :meth:`JobQueue.force_start` ("Process Now")
    bypasses this gate entirely, the same way it already bypasses Auto-
    Processing Pause and the Concurrency Limit -- see that method's own
    docstring. COL-251's :meth:`JobQueue._schedule_default_audio_job` is the
    one production caller that constructs a ``SET_DEFAULT_AUDIO`` Job with a
    non-``None`` ``scheduled_at`` -- the downmix-triggered case.

    ``expected_stream_count`` (COL-251) is set alongside ``scheduled_at`` by
    that same caller: the file's total audio-stream count immediately after
    the downmix remux that triggered this Job (:attr:`~collapsarr.downmix.
    pipeline.PipelineResult.final_stream_count`). ``None`` for every other
    trigger. :meth:`JobQueue._run_job` forwards it to
    ``default_audio_pipeline_runner`` so the COL-247 mechanism-selection
    gate's Plex-write path can detect "Plex hasn't finished re-ingesting the
    new stream yet" as a distinct failure rather than silently resolving
    against a stale stream list -- see :mod:`collapsarr.plex.
    default_audio_write`'s ``PlexDefaultAudioOutcome.STREAM_NOT_YET_INGESTED``.

    ``explicit_stream_index`` (COL-253) is the second, distinct way a
    ``SET_DEFAULT_AUDIO`` job's target stream can be chosen: set only by the
    file detail page's per-row "Set Default Audio" trigger
    (:meth:`~collapsarr.jobs.scheduler.JobScheduler.trigger_set_default_audio`'s
    ``stream_index`` parameter), ``None`` for every other trigger (the
    whole-file manual trigger, the bulk trigger, and every downmix-triggered
    automatic job -- all of which keep resolving their target via
    ``preference`` against the persisted Preferred Default Audio setting,
    unchanged). It is **not** the raw ffprobe ``index`` the caller supplied
    -- it is that stream's *ordinal position within the audio-only stream
    list* (0-based), already translated by :meth:`~collapsarr.jobs.
    scheduler.JobScheduler.trigger_set_default_audio` at trigger time (see
    its docstring for why: a Plex-reported stream carries no ffprobe-index
    counterpart, but both sources enumerate a file's audio streams in the
    same underlying container order, so ordinal position is the one
    identifier that means the same physical track to both
    :func:`~collapsarr.downmix.default_audio_pipeline.run_default_audio_pipeline`
    and :func:`~collapsarr.plex.default_audio_write.apply_default_audio_via_plex`).
    When set, both pipelines select this exact stream directly -- skipping
    ``preference``-based auto-resolution and, crucially, skipping their own
    "already correct" no-op check entirely: an explicit per-track trigger
    always attempts the write, unconditionally, the one behavior this field
    exists to enable (re-triggering a file whose disposition Collapsarr
    already believes is correct, e.g. after a Plex-API write that silently
    failed under the now-fixed COL-252 bug).
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
    cancellation: CancellationHandle | None = None
    scheduled_at: datetime | None = None
    expected_stream_count: int | None = None
    explicit_stream_index: int | None = None


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

#: Signature a ``plex_analyzer`` must match: takes the just-terminated
#: ``Job`` (only ever called for one with ``status is JobStatus.SUCCEEDED``)
#: and triggers a Plex Analyze call for its file, when Plex is configured and
#: the file resolves to a ratingKey (see :func:`collapsarr.jobs.plex_analyze.
#: make_plex_analyzer`, COL-211). Must never raise -- see
#: :meth:`JobQueue._trigger_plex_analyze`.
PlexAnalyzer = Callable[[Job], None]

#: Signature a job-terminal hook must match: called after *every* Job reaches
#: a terminal status -- ``SUCCEEDED`` or ``FAILED`` alike, unconditionally
#: (unlike ``failure_notifier``/``tracked_media_recorder``/``plex_analyzer``,
#: each gated on one specific terminal status).
#: :class:`~collapsarr.jobs.scheduler.JobScheduler`
#: wires its :meth:`~collapsarr.jobs.scheduler.JobScheduler.top_up` here
#: (COL-171), so a Job finishing -- for any reason -- immediately re-tops-up
#: the Auto-Queue Limit's budget if a slot just freed. Must be safe to call
#: concurrently (same requirement as the three recorders above) and must
#: never raise -- see :meth:`JobQueue._call_job_terminal_hook`, which
#: defensively swallows any exception the hook raises so a hook problem can
#: never fail the Job it just finished, or the worker thread running it.
JobTerminalHook = Callable[[Job], None]


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

    ``plex_analyzer``, when set, is called the same way -- same worker
    thread, right after the job reaches its terminal status -- but only for
    a job whose terminal status is ``SUCCEEDED`` (COL-211). See
    :func:`collapsarr.jobs.plex_analyze.make_plex_analyzer` for the
    constructor that resolves the file's Plex ratingKey and issues a Plex
    Analyze call for it, when Plex is configured and the file resolves; it
    must be safe to call concurrently for the same reason the other three
    hooks must, and -- like ``failure_notifier`` -- any exception it raises
    is swallowed by :meth:`_trigger_plex_analyze` so a Plex-side problem can
    never fail the job it is reporting on.

    ``default_audio_pipeline_runner`` (COL-155) is the second
    constructor-injected runner: it runs a ``SET_DEFAULT_AUDIO`` job's
    disposition-only fix (:func:`~collapsarr.downmix.default_audio_pipeline.
    run_default_audio_pipeline`) the same way ``pipeline_runner`` runs a
    ``DOWNMIX`` job's pipeline. Both kinds share every other seam on this
    class -- ``max_concurrency``, ``history_recorder``, ``failure_notifier``,
    ``tracked_media_recorder``, ``plex_analyzer`` -- so a
    ``SET_DEFAULT_AUDIO`` job is visible in job history, dispatches a failure
    notification, and is bounded by the same concurrency cap exactly like a
    ``DOWNMIX`` job. It is not, however, a source of *new* tracked-media
    targets (it never adds a track), so :meth:`_record_tracked_media` is a
    no-op for it in practice (its :attr:`~collapsarr.downmix.pipeline.
    PipelineResult.tracks_added` is always empty); ``plex_analyzer`` fires
    for it exactly as it does for a ``DOWNMIX`` job, though, since a Default
    Audio Track fix also rewrites the file's stream-level metadata Plex has
    cached.

    ``pause_check`` (COL-226, "Auto-Processing Pause") is a zero-arg
    :data:`AutoProcessingPauseCheck` callable :meth:`_claim_next` consults on
    every claim attempt: while it returns ``True``, a free worker never
    claims a new ``PENDING`` Job, so nothing new starts running -- a Job a
    worker has already claimed keeps running to completion regardless (this
    seam only ever guards the claim step). Defaults to ``None``, which reads
    as "never paused" -- the right default for lightweight unit construction
    (e.g. COL-20's concurrency tests) that never touches Settings, mirroring
    every other optional seam on this class. :meth:`from_settings` wires a
    real one (see :func:`_make_live_pause_check`) reading
    ``GlobalSettings.auto_processing_paused`` live, so a ``PUT
    /api/settings`` change takes effect on the very next claim attempt with
    no restart.

    ``now`` (COL-242) is the injectable clock :meth:`_claim_next` compares a
    still-``PENDING`` Job's :attr:`Job.scheduled_at` against -- mirroring the
    pattern :class:`~collapsarr.jobs.scheduler.JobScheduler` already uses for
    its own ``now`` constructor parameter (same signature, same real default,
    see that class's docstring). Defaults directly to :func:`_utcnow` (not the
    ``None``-defers-to-real-implementation pattern ``history_recorder``/
    ``failure_notifier``/etc. use above) because, like ``JobScheduler.now``,
    it needs no DB/session wiring to be real -- there is nothing lightweight
    unit construction needs to opt out of. It also computes
    ``default_audio_auto_schedule``'s ``scheduled_at`` below (``now() + delay``).

    ``default_audio_auto_schedule`` (COL-251), when set, arms
    :meth:`_schedule_default_audio_job`: a ``DOWNMIX`` Job that reaches
    ``SUCCEEDED`` having actually added a track (:attr:`~collapsarr.downmix.
    pipeline.PipelineResult.tracks_added` non-empty) enqueues a follow-up
    ``SET_DEFAULT_AUDIO`` Job for the same file, via :meth:`enqueue_default_audio`,
    with ``scheduled_at`` set to ``now() + default_audio_auto_schedule.delay``
    and ``expected_stream_count`` set to the just-finished Job's
    ``result.final_stream_count``. ``None`` (the default) means the feature
    is off -- a ``DOWNMIX`` Job's success schedules nothing, matching every
    Job kind/trigger's behaviour before this field existed. See
    :class:`DefaultAudioAutoSchedule`.
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
        plex_analyzer: PlexAnalyzer | None = None,
        pause_check: AutoProcessingPauseCheck | None = None,
        now: Callable[[], datetime] = _utcnow,
        default_audio_auto_schedule: DefaultAudioAutoSchedule | None = None,
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
        self._plex_analyzer = plex_analyzer
        #: The Auto-Processing Pause gate (COL-226) -- see the class
        #: docstring's ``pause_check`` paragraph and :data:`AutoProcessingPauseCheck`.
        self._pause_check = pause_check
        #: The injectable clock (COL-242) -- see the class docstring's ``now``
        #: paragraph. Compared against a still-``PENDING`` Job's
        #: ``scheduled_at`` in :meth:`_claim_next`/:meth:`_lowest_priority_pending_locked`.
        self._now = now
        #: The downmix-triggered delayed Default Audio Track Job feature
        #: (COL-251) -- see the class docstring's ``default_audio_auto_schedule``
        #: paragraph and :meth:`_schedule_default_audio_job`.
        self._default_audio_auto_schedule = default_audio_auto_schedule
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
        #: Callback invoked after every Job reaches a terminal status, success or
        #: failure alike (COL-171) -- ``None`` until
        #: :meth:`set_job_terminal_hook` is called. Late-bound via a setter
        #: rather than a constructor argument because
        #: :class:`~collapsarr.jobs.scheduler.JobScheduler` (its only
        #: caller) takes an already-constructed :class:`JobQueue` as its own
        #: first constructor argument -- the queue has to exist before the
        #: scheduler that wires the hook does. See :meth:`set_job_terminal_hook`.
        self._job_terminal_hook: JobTerminalHook | None = None
        #: The persistent worker pool (COL-164). Started explicitly by a
        #: :meth:`start` call (never implicitly on enqueue), then lives until
        #: :meth:`shutdown` (or process exit -- the threads are daemons).
        #: :meth:`_enqueue` does *not* start it; it only ``notify_all``s to
        #: wake any workers already waiting for a claimable Job.
        self._workers: list[threading.Thread] = []
        self._started = False
        #: One dedicated, ad-hoc thread per :meth:`force_start` call (COL-229,
        #: "Process Now") -- unlike :attr:`_workers` (the fixed pool, sized
        #: once at :meth:`start` and never resized), this list grows for the
        #: lifetime of the queue, one entry per force-started job, so
        #: :meth:`shutdown` can join every such thread too, not only the pool
        #: it already knew about at construction time. Appended to (and its
        #: thread started) under ``self._cond`` by :meth:`force_start`, so a
        #: concurrent :meth:`shutdown` never snapshots this list mid-append
        #: or joins a thread that hasn't started yet; never otherwise pruned
        #: (a finished thread's ``join()``
        #: returns immediately, so a growing list of already-finished
        #: :class:`~threading.Thread` objects costs a shutdown-time no-op
        #: join each, not a hang) -- see that method's own docstring.
        self._force_start_threads: list[threading.Thread] = []
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
        default_audio_pipeline_runner: DefaultAudioPipelineRunner | None = None,
        history_recorder: HistoryRecorder | None = None,
        failure_notifier: FailureNotifier | None = None,
        tracked_media_recorder: TrackedMediaRecorder | None = None,
        plex_analyzer: PlexAnalyzer | None = None,
        pause_check: AutoProcessingPauseCheck | None = None,
        now: Callable[[], datetime] = _utcnow,
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
        ``failure_notifier``/``tracked_media_recorder``/``plex_analyzer``
        default to ``None`` -- the right default for lightweight unit
        construction that doesn't want DB writes, e.g. COL-20's concurrency
        tests), this factory is the production path: when any isn't passed
        explicitly, it defaults to a *real* one -- ``history_recorder`` via
        :func:`collapsarr.jobs.history.make_history_recorder`,
        ``failure_notifier`` via :func:`collapsarr.jobs.failure_notify.
        make_failure_notifier`, ``tracked_media_recorder`` via
        :func:`collapsarr.jobs.tracked_media.make_tracked_media_recorder`,
        and ``plex_analyzer`` via :func:`collapsarr.jobs.plex_analyze.
        make_plex_analyzer` -- all four bound to the same session factory for
        ``resolved``'s database (schema brought up to head via
        :func:`~collapsarr.migrations.upgrade_to_head` if not already
        current) -- rather than staying ``None``. This mirrors how
        ``pipeline_runner`` already defaults to the real
        :func:`~collapsarr.downmix.pipeline.run_downmix_pipeline` in the raw
        ``__init__``: a bare ``JobQueue.from_settings()`` call, with no extra
        plumbing, persists history, dispatches failure notifications, keeps
        tracked media (the Wanted view's data source, COL-95) up to date, and
        triggers a Plex Analyze call on success (COL-211) for real. Pass any
        of the four explicitly (or ``None`` isn't obtainable here -- construct
        via :meth:`__init__` directly instead) to opt out.

        This factory is also where the persisted **Preferred Default Audio**
        settings reach a real downmix job. Through COL-247's Phase 1, that
        meant folding ``auto_set_default_audio``/``default_audio_preference``
        into ``pipeline_kwargs`` (COL-152's in-band fix, still available on
        :func:`~collapsarr.downmix.pipeline.run_downmix_pipeline` itself for
        direct/test callers, but no longer wired in here). COL-251 replaces
        that wiring: it reads ``GlobalSettings.auto_set_default_audio``,
        ``default_audio_language``/``default_audio_channel_tier`` (adapted via
        :func:`~collapsarr.settings.service.as_default_audio_preference`), and
        ``default_audio_delay_minutes``, and -- only when the toggle is on and
        a complete preference is configured -- builds a
        :class:`DefaultAudioAutoSchedule` and passes it as
        ``default_audio_auto_schedule`` below. A real ``DOWNMIX`` job that adds
        a new stream then schedules a separate, delayed ``SET_DEFAULT_AUDIO``
        Job for the same file (:meth:`JobQueue._schedule_default_audio_job`)
        rather than fixing the disposition in the same remux -- see that
        method's docstring for why (Plex needs time to ingest the new stream
        before a direct-write mechanism can see it). With the toggle off (the
        default), ``default_audio_auto_schedule`` stays ``None`` and a
        ``DOWNMIX`` job's success schedules nothing, matching pre-COL-251
        behaviour. See :meth:`_resolve_pipeline_kwargs` (now ``ffmpeg_path``
        only) and :meth:`_resolve_default_audio_auto_schedule`.

        ``default_audio_pipeline_runner`` (COL-155), when not passed
        explicitly (``None``, the default here -- unlike the raw
        :meth:`__init__`, where it defaults directly to the real
        :func:`~collapsarr.downmix.default_audio_pipeline.
        run_default_audio_pipeline` for lightweight unit construction),
        resolves to the real COL-247 mechanism-selection gate
        (:func:`~collapsarr.jobs.default_audio_dispatch.
        make_default_audio_pipeline_runner`, bound to this same
        ``session_factory``): a ``SET_DEFAULT_AUDIO`` job dispatched through a
        real queue routes to a direct Plex API write when a Plex Connection
        is configured, and to the ffmpeg remux fallback otherwise -- never
        both for the same file. Pass an explicit ``default_audio_pipeline_runner``
        (e.g. :func:`run_default_audio_pipeline` itself, or a test fake) to
        bypass the gate entirely, the same opt-out every other ``None``-defaulted
        hook on this factory gives. The manual/bulk ``SET_DEFAULT_AUDIO``
        preference itself still isn't read from Settings here: :meth:`JobScheduler.
        trigger_set_default_audio` resolves it live, per call, and passes it
        straight to :meth:`enqueue_default_audio`.

        The database engine backing all of the above is created at most once,
        here, for this :class:`JobQueue` instance -- shared between
        ``history_recorder``, ``failure_notifier``,
        ``tracked_media_recorder``, ``plex_analyzer``, and the Default-Audio
        settings read, but not shared with the FastAPI app's own
        request-scoped engine (see :mod:`collapsarr.main`). For SQLite (this
        project's only supported backend today) that's safe -- both point at
        the same on-disk file -- but it does mean calling this factory
        repeatedly opens a new engine each time, so production code should
        call it once and hold onto the resulting :class:`JobQueue` (e.g. on
        ``app.state``), the same way it already holds onto one session
        factory.

        The imports of :mod:`collapsarr.jobs.history`,
        :mod:`collapsarr.jobs.failure_notify`, :mod:`collapsarr.jobs.
        tracked_media`, and :mod:`collapsarr.jobs.plex_analyze` below are
        deferred (inside this method, not at module scope) because those
        modules import *this* one (for :class:`Job`/:class:`JobStatus`) -- a
        deferred import to break the module cycle, the same reason the
        schema/engine helpers above are imported inside this method rather
        than at module scope.

        ``pause_check`` (COL-226) defaults to a real
        :func:`_make_live_pause_check` bound to this same ``session_factory``
        when not passed explicitly -- unlike the raw :meth:`__init__` (where
        it defaults to ``None``, "never paused"), this factory is the
        production path, so a bare ``JobQueue.from_settings()`` call
        genuinely honours a persisted Auto-Processing Pause, mirroring how
        ``history_recorder``/``failure_notifier``/``tracked_media_recorder``/
        ``plex_analyzer`` above all resolve to real ones here too.

        ``now`` (COL-242) passes straight through to :meth:`__init__` --
        unlike the four hooks above, it needs no ``session_factory`` to be
        real (:func:`_utcnow` touches nothing), so there is no separate
        ``resolved_now``/``None``-defers-to-real step here.
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
        # sizes the pool below, and auto_set_default_audio/default_audio_delay_minutes
        # (COL-251) feed _resolve_default_audio_auto_schedule -- both come off
        # this same singleton row, so one read serves both rather than opening
        # a second session.
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

        resolved_plex_analyzer = plex_analyzer
        if resolved_plex_analyzer is None:
            from collapsarr.jobs.plex_analyze import make_plex_analyzer

            resolved_plex_analyzer = make_plex_analyzer(session_factory)

        resolved_default_audio_pipeline_runner = default_audio_pipeline_runner
        if resolved_default_audio_pipeline_runner is None:
            from collapsarr.jobs.default_audio_dispatch import make_default_audio_pipeline_runner

            resolved_default_audio_pipeline_runner = make_default_audio_pipeline_runner(
                session_factory
            )

        resolved_pause_check = pause_check
        if resolved_pause_check is None:
            resolved_pause_check = _make_live_pause_check(session_factory)

        return cls(
            max_concurrency=global_settings.concurrency_limit,
            pipeline_runner=pipeline_runner,
            pipeline_kwargs=cls._resolve_pipeline_kwargs(pipeline_kwargs, global_settings),
            default_audio_pipeline_runner=resolved_default_audio_pipeline_runner,
            history_recorder=resolved_history_recorder,
            failure_notifier=resolved_failure_notifier,
            tracked_media_recorder=resolved_tracked_media_recorder,
            plex_analyzer=resolved_plex_analyzer,
            pause_check=resolved_pause_check,
            now=now,
            default_audio_auto_schedule=cls._resolve_default_audio_auto_schedule(global_settings),
        )

    @staticmethod
    def _resolve_pipeline_kwargs(
        pipeline_kwargs: Mapping[str, Any] | None,
        global_settings: GlobalSettings,
    ) -> dict[str, Any]:
        """Fold persisted Settings-driven overrides into ``pipeline_kwargs``.

        Takes ``global_settings`` -- the same singleton
        :class:`~collapsarr.settings.models.GlobalSettings` row
        :meth:`from_settings` already reads once for ``concurrency_limit``
        (COL-165), reused here rather than opening a second session.

        **Only** when ``ffmpeg_path`` is set (COL-218 -- e.g. an operator has
        pointed Collapsarr at a runtime-free native FFmpeg build, Epic COL-214),
        threads it into the kwargs both
        :func:`~collapsarr.downmix.pipeline.run_downmix_pipeline` and
        :func:`~collapsarr.downmix.default_audio_pipeline.
        run_default_audio_pipeline` already accept (``ffmpeg_path: str =
        _DEFAULT_FFMPEG_PATH``), so a real job dispatched through this queue
        invokes that FFmpeg binary instead of the bare ``"ffmpeg"`` resolved off
        ``PATH``.

        Until COL-251, this also folded ``auto_set_default_audio``/
        ``default_audio_preference`` in when that toggle was on (COL-152's
        in-band fix) -- that wiring moved to
        :meth:`_resolve_default_audio_auto_schedule` (see :meth:`from_settings`'s
        docstring for why); this method now only ever adds ``ffmpeg_path``.

        With the toggle at its default (unset -- every fresh install's state,
        and every existing install's row after the additive migration)
        nothing is added, so a job's pipeline call is byte-for-byte what it
        was before the feature. An explicit ``pipeline_kwargs`` from the
        caller always wins -- a key already present is never overwritten --
        so a test (or a future alternate wiring) can still pin its own value.
        """
        resolved = dict(pipeline_kwargs or {})
        if global_settings.ffmpeg_path:
            resolved.setdefault("ffmpeg_path", global_settings.ffmpeg_path)
        return resolved

    @staticmethod
    def _resolve_default_audio_auto_schedule(
        global_settings: GlobalSettings,
    ) -> DefaultAudioAutoSchedule | None:
        """Build the downmix-triggered auto-schedule config from Settings, if armed (COL-251).

        Takes ``global_settings`` -- the same singleton row :meth:`from_settings`
        already reads once, reused here rather than opening a second session.

        Returns ``None`` -- "feature off" -- unless *both*:

        - ``global_settings.auto_set_default_audio`` (COL-151) is ``True``; and
        - :func:`~collapsarr.settings.service.as_default_audio_preference`
          resolves an actual preference (``None`` when either
          ``default_audio_language``/``default_audio_channel_tier`` is unset
          -- "no actionable preference", the same guard COL-152's in-band fix
          used).

        Otherwise returns a :class:`DefaultAudioAutoSchedule` pairing that
        preference with ``timedelta(minutes=global_settings.
        default_audio_delay_minutes)`` (COL-243's setting).

        The import below is deferred, matching the surrounding factory: the
        settings service pulls in the ORM/adapters, which don't need to load
        for a lightweight :meth:`__init__` construction that never touches
        Settings.
        """
        if not global_settings.auto_set_default_audio:
            return None
        from collapsarr.settings.service import as_default_audio_preference

        preference = as_default_audio_preference(global_settings)
        if preference is None:
            return None
        return DefaultAudioAutoSchedule(
            preference=preference,
            delay=timedelta(minutes=global_settings.default_audio_delay_minutes),
        )

    @property
    def max_concurrency(self) -> int:
        """The configured cap on simultaneously running jobs."""
        return self._max_concurrency

    def set_job_terminal_hook(self, hook: JobTerminalHook | None) -> None:
        """Set (or clear, with ``None``) the job-terminal hook (COL-171).

        Late-bound rather than constructor-injected -- see the attribute's
        own docstring in :meth:`__init__` for why.
        :class:`~collapsarr.jobs.scheduler.JobScheduler` calls this on its
        own ``queue`` constructor argument, wiring :meth:`~collapsarr.jobs.
        scheduler.JobScheduler.top_up` as the hook -- so production wiring in
        :mod:`collapsarr.main` (which constructs the queue, then the
        scheduler around it) needs no changes of its own to get this: simply
        constructing a ``JobScheduler(job_queue, ...)`` wires it.
        """
        self._job_terminal_hook = hook

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
        self,
        file_path: str | Path,
        preference: DefaultAudioPreference,
        *,
        scheduled_at: datetime | None = None,
        expected_stream_count: int | None = None,
        explicit_stream_index: int | None = None,
    ) -> Job:
        """Add a file + its Default Audio Track preference as a ``SET_DEFAULT_AUDIO`` job (COL-155).

        Mirrors :meth:`enqueue` exactly, for the disposition-only fix: same
        ``PENDING`` job, same worker pool runs it, same ``history_recorder``
        visibility. The job's ``settings`` is a placeholder
        :class:`~collapsarr.downmix.targets.DownmixSettings` with an empty
        ``enabled_targets`` -- unused by :meth:`_run_job` for this kind, but
        keeping it well-formed means job-history's target/language columns
        correctly read as "no downmix target" for this job rather than the
        ``DownmixSettings`` default (Stereo).

        Shares this queue's ``_jobs`` dict (and so its ``max_concurrency``
        cap and, via :class:`~collapsarr.jobs.scheduler.JobScheduler`'s
        file-path-only dedup guard, its de-duplication) with every
        ``DOWNMIX`` job already on it -- the two kinds are not run through
        separate pools.

        ``scheduled_at``/``expected_stream_count`` (COL-251) default to
        ``None`` -- an immediate, claimable-right-away Job with no
        ingestion-wait check, exactly :meth:`enqueue_default_audio`'s
        pre-COL-251 behaviour. Every caller of this method today except
        :meth:`_schedule_default_audio_job` (:class:`~collapsarr.jobs.
        scheduler.JobScheduler`'s manual/bulk ``trigger_set_default_audio``
        trigger) leaves both unset, since a manual/bulk trigger acts on
        streams that already exist in the file -- there is nothing to wait
        on. See :attr:`Job.scheduled_at`/:attr:`Job.expected_stream_count`
        for what each does once the Job runs.

        ``explicit_stream_index`` (COL-253) defaults to ``None`` -- every
        caller except :meth:`~collapsarr.jobs.scheduler.JobScheduler.
        trigger_set_default_audio`'s explicit per-track mode leaves it unset,
        keeping the ordinary preference-based auto-resolve behavior. See
        :attr:`Job.explicit_stream_index` for what a non-``None`` value does
        once the Job runs.
        """
        job = Job(
            file_path=Path(file_path),
            settings=DownmixSettings(enabled_targets=frozenset()),
            kind=JobKind.SET_DEFAULT_AUDIO,
            preference=preference,
            scheduled_at=scheduled_at,
            expected_stream_count=expected_stream_count,
            explicit_stream_index=explicit_stream_index,
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
        late." To hard-kill a job that a worker has *already* claimed (status
        ``RUNNING``), use :meth:`cancel_running` (COL-192) -- this primitive is
        deliberately pending-only, so the bulk "Clear queue" pass
        (:meth:`~collapsarr.jobs.scheduler.JobScheduler.clear_queue`) that
        loops over it keeps its "cancel pending, leave running alone" contract.

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

    def cancel_running(self, job_id: UUID) -> bool:
        """Hard-kill a job a worker is currently running (COL-192).

        The ``RUNNING``-state counterpart of :meth:`cancel`: signals the job's
        :class:`~collapsarr.downmix.cancellation.CancellationHandle` (attached
        in :meth:`_claim_next` when the job was claimed), terminating its live
        ffmpeg/ffprobe subprocess -- and any children, since
        :func:`~collapsarr.downmix.cancellation.make_cancellable_runner` runs
        each in its own process group -- immediately. Returns ``True`` if the
        job was ``RUNNING`` and has now been signalled, ``False`` (not an
        error) if it is unknown or no longer ``RUNNING`` (still ``PENDING``, or
        already terminal): the "finished naturally between the request and the
        kill" race, the same too-late shape :meth:`cancel` reports for the
        pending case.

        The killed subprocess makes the in-flight pipeline call return a
        failure, so the worker transitions the job to
        :attr:`JobStatus.FAILED` and frees its slot through the ordinary
        terminal path (:meth:`_run_job`) -- this method does **not** itself
        touch the job's status, ``_active`` count, or ``JobHistory`` row; it
        only fires the kill and lets the existing machinery run its course. No
        distinct ``CANCELLED`` status is introduced (see the module docstring
        and ``docs/adr/0007``): a hard-cancelled run is recorded as a failure,
        which the ticket permits.

        The handle is signalled outside ``self._lock`` -- killing a subprocess
        can block briefly, and a worker thread needs the lock to make progress
        toward the very terminal transition this cancel is waiting on.
        """
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None or job.status is not JobStatus.RUNNING:
                return False
            handle = job.cancellation
        if handle is not None:
            handle.cancel()
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

    def force_start(self, job_id: UUID) -> bool:
        """Force-start a still-``PENDING`` job immediately, bypassing pause and the concurrency cap.

        The queue-level primitive behind "Process Now" (COL-229,
        ``CONTEXT.md``): unlike the ordinary claim path (:meth:`_claim_next`,
        run only by the pool's fixed ``max_concurrency`` worker threads),
        this claims ``job`` directly -- under the same lock, flipping it
        ``PENDING`` -> ``RUNNING`` exactly the way :meth:`_claim_next` does,
        so no pool worker can race to claim it out from under this call --
        and then runs it (:meth:`_run_job`) on a brand-new, dedicated thread
        rather than waiting for one of the pool's own threads to free up.

        That is what makes this a genuine bypass of every gate "Process Now"
        must clear:

        * **Auto-Processing Pause** (COL-226) -- ``self._pause_check`` is
          consulted only inside :meth:`_claim_next`, never here, so a paused
          queue still force-starts a job through this method.
        * **Concurrency Limit** (COL-165) -- the limit is nothing more than
          "how many worker threads the pool has" (``self._max_concurrency``
          fixed at construction); spinning up one more thread outside that
          fixed pool genuinely runs this job *alongside* however many pool
          workers are already busy, rather than waiting for one to free up.
        * **Scheduled Job due-time gate** (COL-242) -- ``job.scheduled_at`` is
          likewise consulted only inside :meth:`_lowest_priority_pending_locked`
          (called from :meth:`_claim_next`), never here, so a Job scheduled
          for the future still force-starts immediately through this method --
          "Process Now" always means *now*, not "now, unless it was scheduled
          for later."

        Every other side effect of a normal run is unaffected: the new
        thread calls :meth:`_run_job` exactly as a pool worker would, so
        history/tracked-media/failure-notification/Plex-analyze/the
        job-terminal hook (and so the Auto-Queue Limit's top-up, COL-171)
        all fire identically, and :meth:`wait_idle`/:meth:`shutdown` observe
        this job the same way too (``self._active`` is incremented under the
        same lock, right alongside the ``RUNNING`` transition, before the
        new thread is even started). The thread itself is created, appended
        to ``self._force_start_threads``, *and* started, all under that same
        lock (mirroring how :meth:`start` creates and starts every fixed-pool
        worker inside its own ``with self._cond:`` block) -- unlike
        :attr:`_workers` (the fixed pool, sized once at :meth:`start`), this
        list grows one entry per force-started job, so :meth:`shutdown` can
        find and join it too, alongside the pool threads, rather than only
        ever waiting on the pool it already knew about at construction time.
        Starting the thread inside the lock, rather than after releasing it,
        closes a narrow race: a concurrent :meth:`shutdown` that acquired the
        lock, snapshotted this list, and reached its join loop before this
        call got around to ``.start()`` would otherwise call
        :meth:`~threading.Thread.join` on a never-started thread, raising
        ``RuntimeError``.

        Returns ``True`` if ``job_id`` was still ``PENDING`` and has now been
        claimed and started; ``False`` (not an error) if it names no job the
        live queue knows about, or one that is no longer ``PENDING`` (already
        ``RUNNING`` or terminal) -- "too late," mirroring :meth:`bump_to_front`
        (and :meth:`cancel`)'s contract exactly. A caller that already has the
        target job in hand and only cares whether *some* job is now running
        for it (rather than specifically whether *this* call is what started
        it) can treat ``False`` as "already handled" -- see
        :meth:`~collapsarr.jobs.scheduler.JobScheduler.process_now`. Also
        returns ``False`` -- without touching ``job`` at all -- once
        :meth:`shutdown` has begun (``self._shutdown`` set): "after shutdown
        the queue accepts no new work" (see :meth:`shutdown`'s own docstring)
        applies here exactly as it does to a fresh :meth:`enqueue`, and
        checking this under the same lock :meth:`shutdown` sets the flag
        under closes the race where a job could otherwise start running on a
        thread :meth:`shutdown` already took its join-list snapshot without.
        """
        with self._cond:
            if self._shutdown:
                return False
            job = self._jobs.get(job_id)
            if job is None or job.status is not JobStatus.PENDING:
                return False
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(UTC)
            # COL-192: attach a hard-kill handle exactly like _claim_next
            # does, so a force-started job can still be cancelled mid-flight
            # like any other RUNNING job.
            job.cancellation = CancellationHandle()
            self._active += 1  # stays counted until _run_job fully finishes
            thread = threading.Thread(
                target=self._run_job,
                args=(job,),
                name=f"collapsarr-jobworker-forced-{job.id}",
                daemon=True,
            )
            self._force_start_threads.append(thread)
            # Started *inside* the lock -- mirrors :meth:`start` (the fixed
            # pool's own workers are created and started inside its own
            # ``with self._cond:`` block) -- so append-to-list and
            # actually-started happen atomically relative to a concurrent
            # :meth:`shutdown` call's snapshot-then-join. If ``.start()`` ran
            # after releasing the lock instead, a ``shutdown()`` that
            # acquired the lock, took its snapshot (already including this
            # thread, appended above), and reached its join loop in that
            # narrow window -- before this call actually got to ``.start()``
            # -- would call ``Thread.join()`` on a thread that was never
            # started, raising ``RuntimeError``. Thread.start() itself is
            # cheap (just spawns the OS thread; it does not wait for the
            # target to run), so holding the lock a little longer here costs
            # nothing meaningful.
            thread.start()
        return True

    def count_running(self) -> int:
        """Count of currently ``RUNNING`` jobs, any origin -- pool-claimed or force-started.

        Mirrors :meth:`~collapsarr.jobs.scheduler.JobScheduler._count_pending`'s
        shape (a plain filtered count over the live jobs), for the
        ``RUNNING`` status instead of ``PENDING`` -- what
        :meth:`~collapsarr.jobs.scheduler.JobScheduler.would_exceed_concurrency_limit`
        compares against :attr:`max_concurrency` to decide whether "Process
        Now" needs to ask for confirmation before force-starting one more.
        """
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.status is JobStatus.RUNNING)

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

    def wait_no_running(self, timeout: float | None = None) -> bool:
        """Block until no job is currently ``RUNNING`` -- ignoring ``PENDING`` (COL-233).

        The "Wait & Restart" self-update flow's own wait step
        (:mod:`collapsarr.self_update.apply`): unlike :meth:`wait_idle`, this
        deliberately does **not** also wait for every ``PENDING`` job to
        drain. That distinction matters here specifically because the
        self-update apply flow force-sets ``auto_processing_paused`` (COL-226)
        *before* calling this -- while paused, :meth:`_claim_next` never
        claims a new ``PENDING`` job, so a naive :meth:`wait_idle` call would
        block forever on a Job that was already sitting ``PENDING`` (or one a
        concurrent enqueue/top-up added) the moment claiming was paused. This
        method only ever waits on ``self._active`` (the same counter
        :meth:`wait_idle` checks, incremented under the lock the instant a
        worker -- pool or force-started -- claims a Job, decremented only
        once :meth:`_run_job` has fully finished it, success or failure
        alike), which is exactly the set of Jobs an operator's browser tab
        would see as "running" right now.

        Returns ``True`` once no job is ``RUNNING``, or ``False`` if
        ``timeout`` (seconds) elapsed first. With no ``timeout`` it waits
        indefinitely. Returns immediately when nothing is currently
        ``RUNNING``.
        """
        with self._cond:
            if timeout is None:
                while self._active > 0:
                    self._cond.wait()
                return True
            deadline = time.monotonic() + timeout
            while self._active > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._active == 0
                self._cond.wait(remaining)
            return True

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """Stop the worker pool; any job a worker already claimed still finishes.

        Idempotent. Signals every worker to exit once it finishes whatever it
        is currently running -- an orderly *shutdown* still does not interrupt
        an in-flight ``ffmpeg`` (it drains gracefully); the manual, per-job
        hard kill COL-192 added (:meth:`cancel_running`, superseding ADR 0007's
        original blanket "no interruption") is a separate, explicit action, not
        part of shutdown. When ``wait`` (the default), joins both the pool's
        worker threads *and* every still-live :meth:`force_start` (COL-229,
        "Process Now") thread before returning -- a force-started job is a
        genuine in-flight run too (``self._active`` counts it exactly like a
        pool-claimed one), so an orderly shutdown must not return while one is
        still running any more than it would for a pool worker's. After
        shutdown the queue accepts no new work: a later :meth:`enqueue`
        records the job but never starts a pool to run it, and a later
        :meth:`force_start` call returns ``False`` without touching anything
        (see that method's own docstring) rather than spinning up a thread
        this call has no way to know about and join.
        """
        with self._cond:
            if self._shutdown:
                return
            self._shutdown = True
            self._cond.notify_all()
            workers = list(self._workers)
            forced_threads = list(self._force_start_threads)
        if wait:
            for worker in workers:
                worker.join(timeout=timeout)
            for thread in forced_threads:
                thread.join(timeout=timeout)

    def __enter__(self) -> JobQueue:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    def _worker_loop(self) -> None:
        """One pool worker: claim the next job, run it, repeat until shutdown (COL-164).

        ``_run_job`` is designed to never raise -- every side effect it runs
        after the pipeline result is known goes through
        :meth:`_run_side_effect`, which catches and logs on ``job``'s behalf
        (COL-254) -- but this loop wraps the call in its own defensive
        ``try``/``except`` anyway, as a last-resort backstop: a worker thread
        silently vanishing from the pool (because some future change to
        ``_run_job`` reintroduces an unguarded call, say) would be far worse
        than logging an unexpected exception and continuing to the next
        claim. Deliberately broad -- anything that reaches here is already a
        bug in ``_run_job``, not an expected outcome to discriminate on.
        """
        while True:
            job = self._claim_next()
            if job is None:  # shutdown signalled while idle
                return
            try:
                self._run_job(job)
            except Exception:  # noqa: BLE001 - a worker thread must never silently disappear
                logger.exception(
                    "job %s: _run_job raised unexpectedly -- worker continuing", job.id
                )

    def _claim_next(self) -> Job | None:
        """Block until a job is claimable, atomically claim it, and return it.

        Returns the lowest-``priority`` still-``PENDING`` job, flipped to
        ``RUNNING`` under the lock so no other worker can claim it and a
        concurrent :meth:`cancel` correctly loses the race (sees it already
        non-``PENDING``). Returns ``None`` only when :meth:`shutdown` was
        signalled while this worker was idle -- the worker's cue to exit.

        **Auto-Processing Pause (COL-226).** Before picking a job, checks
        ``self._pause_check()`` (when configured) and, while it returns
        ``True``, treats the claim as unclaimable regardless of what's
        actually ``PENDING`` -- so no free worker starts a new Job while
        paused. A Job a worker already claimed keeps running: this method is
        only ever on the *claim* path, never called again for an
        already-``RUNNING`` Job. While paused, the wait below uses
        :data:`_PAUSE_POLL_INTERVAL_SECONDS` instead of blocking forever, so
        toggling the setting back off is picked up within that bound even
        though nothing explicitly wakes this worker (see that constant's
        docstring for why) -- once claimable, the ordinary indefinite wait
        (woken by :meth:`_enqueue`/:meth:`bump_to_front`/:meth:`shutdown`)
        applies as before.

        **Scheduled Job due-time gate (COL-242).** :meth:`_lowest_priority_pending_locked`
        already excludes a still-``PENDING`` Job whose :attr:`Job.scheduled_at`
        is in the future (compared against ``self._now()``), so this method
        never sees it as claimable either way -- it simply isn't returned.
        When that is the *only* reason nothing is claimable (not paused, but
        every ``PENDING`` Job is scheduled for later), the wait below uses
        :data:`_SCHEDULE_POLL_INTERVAL_SECONDS` instead of blocking forever,
        for the identical reason the pause case above does: nothing wakes a
        blocked worker merely because wall-clock time passed a Job's due
        time. See :meth:`_has_future_scheduled_pending_locked`.
        """
        with self._cond:
            while True:
                if self._shutdown:
                    return None
                paused = self._pause_check is not None and self._pause_check()
                if not paused:
                    job = self._lowest_priority_pending_locked()
                    if job is not None:
                        job.status = JobStatus.RUNNING
                        job.started_at = datetime.now(UTC)
                        # COL-192: attach a hard-kill handle under the same lock
                        # that flips the job to RUNNING, so a concurrent
                        # cancel_running() either sees it here (and kills the
                        # subprocess once _run_job attaches one) or loses the race
                        # cleanly against a job that already finished.
                        job.cancellation = CancellationHandle()
                        self._active += 1  # stays counted until _run_job fully finishes
                        return job
                if paused:
                    wait_seconds: float | None = _PAUSE_POLL_INTERVAL_SECONDS
                elif self._has_future_scheduled_pending_locked():
                    wait_seconds = _SCHEDULE_POLL_INTERVAL_SECONDS
                else:
                    wait_seconds = None
                self._cond.wait(wait_seconds)

    def _lowest_priority_pending_locked(self) -> Job | None:
        """The pending job a free worker should claim next (call with the lock held).

        A linear scan for the minimum ``priority`` among ``PENDING`` jobs
        that are also currently *claimable* -- ``scheduled_at`` unset, or set
        to a moment that has already passed ``self._now()`` (COL-242). A
        ``PENDING`` Job whose ``scheduled_at`` is still in the future is
        excluded from consideration entirely, exactly as if it weren't
        pending yet -- it stays ``PENDING`` and reappears in this scan on the
        next claim attempt once its due time passes. The queue holds
        media-library-scale job counts, so an O(n) pick per claim is simpler
        and less error-prone than a heap that would also have to support
        :meth:`cancel`'s arbitrary removal and :meth:`bump_to_front`'s key
        decrease.
        """
        now = self._now()
        claimable = [
            job
            for job in self._jobs.values()
            if job.status is JobStatus.PENDING
            and (job.scheduled_at is None or job.scheduled_at <= now)
        ]
        if not claimable:
            return None
        return min(claimable, key=lambda job: job.priority)

    def _has_future_scheduled_pending_locked(self) -> bool:
        """Whether some ``PENDING`` Job is blocked purely by a future ``scheduled_at`` (COL-242).

        Called with the lock held, only once :meth:`_lowest_priority_pending_locked`
        has already reported nothing claimable and the queue isn't paused --
        distinguishes "genuinely nothing pending" (the ordinary indefinite
        wait applies, woken only by :meth:`_enqueue`/:meth:`bump_to_front`/
        :meth:`shutdown`) from "something is pending but not due yet" (the
        bounded :data:`_SCHEDULE_POLL_INTERVAL_SECONDS` wait applies instead,
        so a worker notices the due time passing without needing an explicit
        wake). In production this is always ``False`` today -- nothing yet
        enqueues a Job with a non-``None`` ``scheduled_at`` -- so every
        existing trigger keeps using the ordinary indefinite wait unchanged.
        """
        now = self._now()
        return any(
            job.status is JobStatus.PENDING
            and job.scheduled_at is not None
            and job.scheduled_at > now
            for job in self._jobs.values()
        )

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
        ``self._notify_failure`` (a no-op unless the job actually
        ``FAILED`` and a ``failure_notifier`` is configured),
        ``self._trigger_plex_analyze`` (a no-op unless the job actually
        ``SUCCEEDED`` and a ``plex_analyzer`` is configured, COL-211), and
        ``self._schedule_default_audio_job`` (a no-op unless the job actually
        ``SUCCEEDED`` as a ``DOWNMIX`` job that added a track and
        ``default_audio_auto_schedule`` is configured, COL-251) run -- all
        outside ``self._lock``, since by that point only this thread ever
        touches this particular ``job`` (each job is claimed by exactly one
        worker), so there is nothing left to race against.

        Every one of those calls (both the ``RUNNING`` ``_record_history``
        below and every post-pipeline one further down) is routed through
        :meth:`_run_side_effect` rather than called directly (COL-254): a
        recorder/notifier/hook raising must never (a) skip a *later* side
        effect in the same sequence -- most importantly the terminal
        ``_record_history`` write, which is what :func:`~collapsarr.jobs.
        history.list_queue_jobs` (and so the Queue page) reads, so skipping
        it is what left a job showing "Running" for 48+ hours after it had
        actually finished -- or (b) propagate out of this method and kill the
        calling worker thread. See :meth:`_run_side_effect`'s own docstring
        for the swallow-and-log contract. This never touches ``job.status``
        itself: a side-effect failure only ever prevents that one side effect
        (and its own failure) from cascading, never flips a
        already-determined ``SUCCEEDED``/``FAILED`` outcome.

        Dispatches on ``job.kind`` (COL-155) for which runner actually
        executes the pipeline: ``DOWNMIX`` calls ``self._pipeline_runner``
        with ``job.settings`` and the full ``self._pipeline_kwargs``,
        ``SET_DEFAULT_AUDIO`` calls ``self._default_audio_pipeline_runner``
        with ``job.preference``, ``job.expected_stream_count`` (COL-251),
        ``job.explicit_stream_index`` (COL-253), and
        only the :data:`_SHARED_DEFAULT_AUDIO_PIPELINE_KWARGS` subset of
        ``self._pipeline_kwargs`` (COL-218 -- today, just ``ffmpeg_path``; see
        that constant's docstring for why the *whole* dict can't be forwarded)
        -- everything else below (history/tracked-media/failure handling) is
        identical for both kinds.
        """
        try:
            self._run_side_effect(job, "_record_history", lambda: self._record_history(job))
            logger.info(
                "job %s started: kind=%s file=%s %s",
                job.id,
                job.kind.value,
                job.file_path,
                _run_context_for_log(job),
            )

            try:
                # COL-192: thread the job's hard-kill handle into whichever
                # pipeline runs, so its ffmpeg/ffprobe subprocesses register
                # with it and a concurrent cancel_running() can terminate them.
                # The real pipeline runners accept `cancel_handle`; injected
                # test stubs swallow it via **kwargs.
                if job.kind is JobKind.SET_DEFAULT_AUDIO:
                    assert job.preference is not None  # enqueue_default_audio always sets this
                    shared_kwargs = {
                        key: value
                        for key, value in self._pipeline_kwargs.items()
                        if key in _SHARED_DEFAULT_AUDIO_PIPELINE_KWARGS
                    }
                    result = self._default_audio_pipeline_runner(
                        job.file_path,
                        job.preference,
                        cancel_handle=job.cancellation,
                        # COL-251: per-Job, not part of `_pipeline_kwargs` --
                        # see `_SHARED_DEFAULT_AUDIO_PIPELINE_KWARGS`'s docstring.
                        expected_stream_count=job.expected_stream_count,
                        # COL-253: likewise per-Job -- see `Job.explicit_stream_index`.
                        explicit_stream_index=job.explicit_stream_index,
                        **shared_kwargs,
                    )
                else:
                    result = self._pipeline_runner(
                        job.file_path,
                        job.settings,
                        cancel_handle=job.cancellation,
                        **self._pipeline_kwargs,
                    )
            except Exception as exc:  # noqa: BLE001 - captured as the job's outcome, not re-raised
                with self._lock:
                    job.error = exc
                    job.status = JobStatus.FAILED
                    job.ended_at = datetime.now(UTC)
                logger.exception("job %s failed with an unexpected error", job.id)
                self._run_side_effect(job, "_record_history", lambda: self._record_history(job))
                self._run_side_effect(
                    job, "_record_tracked_media", lambda: self._record_tracked_media(job)
                )
                self._run_side_effect(job, "_notify_failure", lambda: self._notify_failure(job))
                self._run_side_effect(
                    job, "_trigger_plex_analyze", lambda: self._trigger_plex_analyze(job)
                )
                self._run_side_effect(
                    job, "_call_job_terminal_hook", lambda: self._call_job_terminal_hook(job)
                )
                return

            with self._lock:
                job.result = result
                job.status = JobStatus.SUCCEEDED if result.success else JobStatus.FAILED
                job.ended_at = datetime.now(UTC)
            if job.status is JobStatus.SUCCEEDED:
                logger.info("job %s completed: file=%s -- %s", job.id, job.file_path, result.detail)
            self._run_side_effect(job, "_record_history", lambda: self._record_history(job))
            self._run_side_effect(
                job, "_record_tracked_media", lambda: self._record_tracked_media(job)
            )
            self._run_side_effect(job, "_notify_failure", lambda: self._notify_failure(job))
            self._run_side_effect(
                job, "_trigger_plex_analyze", lambda: self._trigger_plex_analyze(job)
            )
            self._run_side_effect(
                job, "_schedule_default_audio_job", lambda: self._schedule_default_audio_job(job)
            )
            self._run_side_effect(
                job, "_call_job_terminal_hook", lambda: self._call_job_terminal_hook(job)
            )
        finally:
            # Only now -- after every side effect -- is the job fully done, so
            # this is where wait_idle is allowed to observe it as no longer
            # active. The ``finally`` guarantees the count is released (and
            # waiters woken) even if a recorder/notifier raised unexpectedly,
            # so a stray side-effect error can never wedge wait_idle/shutdown.
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def _run_side_effect(self, job: Job, label: str, effect: Callable[[], None]) -> None:
        """Run one post-pipeline (or pre-pipeline ``RUNNING``) side effect for ``job`` (COL-254).

        Every call :meth:`_run_job` makes into a recorder/notifier/hook --
        ``_record_history``, ``_record_tracked_media``, ``_notify_failure``,
        ``_trigger_plex_analyze``, ``_schedule_default_audio_job``, and
        ``_call_job_terminal_hook`` -- goes through this one helper instead of
        being called directly, so the same safety net applies uniformly
        rather than being (re)implemented -- and potentially missed -- at
        each of those six call sites individually. ``effect`` is expected to
        take no arguments (``job`` is closed over by the caller) purely so
        this stays a single, reusable one-liner per call site.

        Catches and logs (via ``logger.exception``, so the traceback survives
        -- never silently swallowed) any exception ``effect`` raises. This is
        deliberately broad: a side effect misbehaving must never be able to
        (a) stop a *later* side effect in the same :meth:`_run_job` call from
        running -- in particular, the terminal ``_record_history`` write,
        whose absence is exactly what left a completed job showing "Running"
        in the Queue page indefinitely (COL-254's reported symptom) -- or (b)
        propagate out of :meth:`_run_job` and kill the calling worker thread,
        permanently shrinking the pool by one. Deliberately does *not* touch
        ``job.status`` in any way -- a side effect's own failure must never
        flip an already-determined ``SUCCEEDED``/``FAILED`` outcome (or vice
        versa); it can only ever fail to apply that one side effect.
        """
        try:
            effect()
        except Exception:  # noqa: BLE001 - a side effect must never fail the job or the worker
            logger.exception("job %s: %s raised", job.id, label)

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
        this); the defensive catch-and-log for the case it does anyway lives
        one level up, in :meth:`_run_side_effect` (COL-254), which every
        caller of this method goes through rather than calling it directly.
        """
        if self._failure_notifier is None or job.status is not JobStatus.FAILED:
            return
        self._failure_notifier(job)

    def _trigger_plex_analyze(self, job: Job) -> None:
        """Trigger a Plex Analyze call for ``job``'s file, if configured and it succeeded (COL-211).

        A no-op for a job that didn't reach ``SUCCEEDED``, or when no
        ``plex_analyzer`` was configured. ``self._plex_analyzer`` is expected
        to never raise on its own (COL-211's :func:`~collapsarr.jobs.
        plex_analyze.trigger_plex_analyze` guarantees this -- it is itself a
        no-op, with no error surfaced, when Plex isn't configured or the file
        doesn't resolve to a ratingKey); the defensive catch-and-log for the
        case it does anyway lives one level up, in :meth:`_run_side_effect`
        (COL-254), mirroring :meth:`_notify_failure` exactly.
        """
        if self._plex_analyzer is None or job.status is not JobStatus.SUCCEEDED:
            return
        self._plex_analyzer(job)

    def _schedule_default_audio_job(self, job: Job) -> None:
        """Enqueue a delayed follow-up ``SET_DEFAULT_AUDIO`` Job for a downmix, if armed (COL-251).

        A no-op unless every one of these holds:

        - :attr:`_default_audio_auto_schedule` is configured (the opt-in
          ``auto_set_default_audio`` toggle is on and a complete preference is
          set -- see :class:`DefaultAudioAutoSchedule`);
        - ``job.kind is JobKind.DOWNMIX`` (a ``SET_DEFAULT_AUDIO`` job never
          schedules another one -- there is no chaining);
        - ``job.status is JobStatus.SUCCEEDED``; and
        - ``job.result.tracks_added`` is non-empty -- the downmix actually
          added a new stream. A ``NOTHING_TO_DO`` downmix (every enabled
          target already present) has nothing new for Plex to ingest, so
          nothing is scheduled for it.

        When armed, calls :meth:`enqueue_default_audio` for the same file with
        the configured preference, ``scheduled_at=self._now() +
        default_audio_auto_schedule.delay``, and ``expected_stream_count`` set
        to ``job.result.final_stream_count`` (the file's total audio-stream
        count immediately after this downmix) -- the input the COL-247
        mechanism-selection gate's Plex-write path (:func:`~collapsarr.plex.
        default_audio_write.apply_default_audio_via_plex`) uses to detect a
        not-yet-ingested stream distinctly. Called from the same worker thread
        as :meth:`_trigger_plex_analyze`, right after it, outside ``self._lock``
        -- safe to call back into :meth:`enqueue_default_audio` (which takes
        the lock itself) for the same reason documented on
        :meth:`_call_job_terminal_hook`. The defensive catch-and-log for a
        problem scheduling the follow-up Job -- which must never be able to
        fail the ``DOWNMIX`` job that just succeeded -- lives one level up,
        in :meth:`_run_side_effect` (COL-254).
        """
        if self._default_audio_auto_schedule is None:
            return
        if job.kind is not JobKind.DOWNMIX or job.status is not JobStatus.SUCCEEDED:
            return
        if job.result is None or not job.result.tracks_added:
            return
        scheduled_at = self._now() + self._default_audio_auto_schedule.delay
        self.enqueue_default_audio(
            job.file_path,
            self._default_audio_auto_schedule.preference,
            scheduled_at=scheduled_at,
            expected_stream_count=job.result.final_stream_count,
        )

    def _call_job_terminal_hook(self, job: Job) -> None:
        """Invoke the job-terminal hook for ``job``, if one is configured (COL-171).

        Unlike :meth:`_notify_failure`/:meth:`_record_tracked_media` (each
        gated on one specific terminal status), this fires for *every*
        terminal ``job`` -- ``SUCCEEDED`` or ``FAILED`` alike -- since
        :class:`~collapsarr.jobs.scheduler.JobScheduler`'s
        :meth:`~collapsarr.jobs.scheduler.JobScheduler.top_up` (the hook
        production wiring installs, via :meth:`set_job_terminal_hook`) needs
        to re-check the Auto-Queue Limit's budget regardless of *why* a slot
        just freed up.

        Called from the worker thread, outside ``self._lock`` -- same as
        ``_record_tracked_media``/``_notify_failure`` above, and for the same
        reason: by this point only this thread still touches ``job``, so a
        hook that itself enqueues more work (as ``top_up`` does) can safely
        call back into this very :class:`JobQueue` (``list_jobs``/``enqueue``)
        without this thread already holding a lock those methods also need --
        no deadlock, no reentrancy. The defensive catch-and-log for a hook
        problem -- which must never fail the job it just finished, or wedge
        the worker loop -- lives one level up, in :meth:`_run_side_effect`
        (COL-254).
        """
        if self._job_terminal_hook is None:
            return
        self._job_terminal_hook(job)
