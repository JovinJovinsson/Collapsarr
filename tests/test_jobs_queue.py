"""Tests for the job queue and bounded-concurrency worker pool (COL-20).

Every test drives :class:`~collapsarr.jobs.queue.JobQueue` with an injected
``pipeline_runner`` stub -- never the real ``ffmpeg``/``ffprobe``-backed
:func:`~collapsarr.downmix.pipeline.run_downmix_pipeline` -- since this
module's job is concurrency control and outcome capture, not the pipeline
itself (that's COL-19's, already covered by ``test_downmix_pipeline.py``).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.downmix.cancellation import CancellationHandle
from collapsarr.downmix.default_audio import DefaultAudioPreference
from collapsarr.downmix.default_audio_pipeline import run_default_audio_pipeline
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget, QualifyingTarget
from collapsarr.jobs.queue import (
    DEFAULT_MAX_CONCURRENCY,
    DefaultAudioAutoSchedule,
    Job,
    JobKind,
    JobQueue,
    JobStatus,
)
from collapsarr.migrations import upgrade_to_head
from collapsarr.settings.service import update_global_settings

_FIXED_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
"""A fixed "now" for injecting into ``JobQueue(now=...)`` (COL-242) -- mirrors
the ``_FIXED_NOW`` pattern already used by ``test_jobs_scheduler.py``/
``test_health_failed_jobs.py`` for their own injected clocks."""

_SUCCESS = PipelineResult(outcome=PipelineOutcome.SUCCESS, success=True, detail="ok")
_NOTHING_TO_DO = PipelineResult(
    outcome=PipelineOutcome.NOTHING_TO_DO, success=True, detail="nothing to do"
)
_FAILED = PipelineResult(
    outcome=PipelineOutcome.REMUX_FAILED, success=False, detail="ffmpeg exited 1"
)


class _StubRunner:
    """A pipeline_runner stub that always returns a fixed result, recording calls."""

    def __init__(self, result: PipelineResult) -> None:
        self._result = result
        self.calls: list[tuple[Path, DownmixSettings]] = []

    def __call__(self, file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        self.calls.append((file_path, settings))
        return self._result


def _stub_runner(result: PipelineResult) -> _StubRunner:
    return _StubRunner(result)


class _GatedRunner:
    """A runner whose ``gate`` job blocks until released, recording run order.

    The pattern the COL-164 reorder/cancel tests need: enqueue a ``gate`` file
    first so the (single) worker claims and blocks on it, leaving the queue's
    other pending jobs sitting claimable while the test reorders/cancels them,
    then :meth:`release` and observe the resulting run order via ``order``.
    """

    def __init__(self, result: PipelineResult = _SUCCESS) -> None:
        self._result = result
        self.started = threading.Event()  # set once the gate job is running
        self.release = threading.Event()  # test sets this to let the gate finish
        self.order: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        if file_path.stem == "gate":
            self.started.set()
            assert self.release.wait(timeout=5), "gate job was never released"
        with self._lock:
            self.order.append(file_path.stem)
        return self._result


# ---------------------------------------------------------------------------
# Enqueueing: file path + target/language context.
# ---------------------------------------------------------------------------


def test_enqueue_creates_a_pending_job_with_file_path_and_settings() -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    settings = DownmixSettings(enabled_targets=frozenset({DownmixTarget.FIVE_POINT_ONE}))

    job = queue.enqueue("/media/movie.mkv", settings)

    assert job.file_path == Path("/media/movie.mkv")
    assert job.settings is settings
    assert job.status is JobStatus.PENDING
    assert job.result is None
    assert job.error is None
    assert queue.list_jobs() == [job]
    assert queue.get_job(job.id) is job


def test_enqueue_assigns_priority_as_a_monotonically_increasing_sequence() -> None:
    """COL-163 AC: priority is a plain 0, 1, 2, ... join-order sequence per queue."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))

    first = queue.enqueue("/media/a.mkv", DownmixSettings())
    second = queue.enqueue("/media/b.mkv", DownmixSettings())
    third = queue.enqueue("/media/c.mkv", DownmixSettings())

    assert (first.priority, second.priority, third.priority) == (0, 1, 2)


def test_priority_assignment_is_thread_safe_under_concurrent_enqueue() -> None:
    """Many threads enqueueing at once each get a distinct, gap-free priority."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    job_count = 50
    jobs: list[Job] = []
    jobs_lock = threading.Lock()

    def worker(index: int) -> None:
        job = queue.enqueue(f"/media/{index}.mkv", DownmixSettings())
        with jobs_lock:
            jobs.append(job)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(job_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    priorities = sorted(job.priority for job in jobs)
    assert priorities == list(range(job_count))  # every value used exactly once, no gaps


def test_default_max_concurrency_is_one() -> None:
    assert DEFAULT_MAX_CONCURRENCY == 1
    assert JobQueue().max_concurrency == 1


def test_rejects_a_non_positive_max_concurrency() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        JobQueue(max_concurrency=0)


# ---------------------------------------------------------------------------
# Execution: each job invokes the pipeline and captures its outcome.
# ---------------------------------------------------------------------------


def test_worker_pool_invokes_the_pipeline_with_the_jobs_file_and_settings() -> None:
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner)
    settings = DownmixSettings()
    queue.enqueue("/media/movie.mkv", settings)

    queue.start()
    queue.wait_idle()

    assert len(queue.list_jobs()) == 1
    assert runner.calls == [(Path("/media/movie.mkv"), settings)]


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        (_SUCCESS, JobStatus.SUCCEEDED),
        (_NOTHING_TO_DO, JobStatus.SUCCEEDED),
        (_FAILED, JobStatus.FAILED),
    ],
)
def test_worker_pool_captures_the_pipeline_outcome_onto_the_job(
    result: PipelineResult, expected_status: JobStatus
) -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(result))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert job.status is expected_status
    assert job.result is result
    assert job.error is None


def test_worker_pool_captures_an_unexpected_runner_exception_as_a_failed_job() -> None:
    def raising_runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        raise RuntimeError("boom")

    queue = JobQueue(pipeline_runner=raising_runner)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert job.status is JobStatus.FAILED
    assert job.result is None
    assert isinstance(job.error, RuntimeError)
    assert str(job.error) == "boom"


def test_wait_idle_returns_immediately_when_nothing_is_enqueued() -> None:
    """A never-used queue is already idle -- wait_idle returns True at once."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))

    assert queue.wait_idle(timeout=1.0) is True


def test_enqueue_before_start_leaves_the_job_pending_until_the_pool_starts() -> None:
    """Enqueue only records a PENDING job; nothing runs until start()."""
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner)

    job = queue.enqueue("/media/a.mkv", DownmixSettings())
    assert job.status is JobStatus.PENDING
    assert runner.calls == []  # no worker pool yet -> nothing has run

    queue.start()
    queue.wait_idle()
    # Re-read through get_job: the worker thread mutated status out from under
    # the local, which the type-checker's narrowing of `job` can't see.
    assert queue.get_job(job.id) is not None
    assert queue.get_job(job.id).status is JobStatus.SUCCEEDED  # type: ignore[union-attr]


def test_a_job_enqueued_while_workers_are_busy_is_picked_up_when_one_frees() -> None:
    """AC: a job enqueued mid-run is claimed as soon as a worker frees -- no batch wait."""
    runner = _GatedRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    queue.enqueue("/media/gate.mkv", DownmixSettings())  # the sole worker claims + blocks
    assert runner.started.wait(timeout=5)

    later = queue.enqueue("/media/later.mkv", DownmixSettings())
    assert later.status is JobStatus.PENDING  # still waiting -- worker is busy on the gate

    runner.release.set()
    assert queue.wait_idle(timeout=5) is True

    assert queue.get_job(later.id) is not None
    assert queue.get_job(later.id).status is JobStatus.SUCCEEDED  # type: ignore[union-attr]
    assert runner.order == ["gate", "later"]


# ---------------------------------------------------------------------------
# wait_no_running (COL-233): "Wait & Restart"'s own wait step -- unlike
# wait_idle, ignores PENDING Jobs entirely (only cares whether anything is
# currently RUNNING).
# ---------------------------------------------------------------------------


def test_wait_no_running_returns_immediately_when_nothing_is_running() -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))

    assert queue.wait_no_running(timeout=1.0) is True


def test_wait_no_running_ignores_a_pending_job_that_will_never_be_claimed() -> None:
    """AC: unlike wait_idle, a PENDING Job blocked by Auto-Processing Pause never matters.

    This is the exact scenario ``apply_with_flow``'s "Wait & Restart" flow
    creates: it force-pauses claiming *before* calling this, so any Job still
    sitting PENDING at that point will never be claimed until the pause
    lifts -- a plain :meth:`wait_idle` call would block forever on it.
    """
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner, pause_check=lambda: True)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    queue.start()

    assert queue.wait_no_running(timeout=1.0) is True  # never blocks on the PENDING job
    assert job.status is JobStatus.PENDING  # still never claimed


def test_wait_no_running_blocks_until_the_running_job_finishes() -> None:
    runner = _GatedRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    queue.enqueue("/media/gate.mkv", DownmixSettings())  # the sole worker claims + blocks
    assert runner.started.wait(timeout=5)

    result: dict[str, bool] = {}

    def _wait() -> None:
        result["done"] = queue.wait_no_running(timeout=5)

    thread = threading.Thread(target=_wait)
    thread.start()
    time.sleep(0.1)  # give the waiter a moment to actually start blocking
    assert "done" not in result  # still running -- the wait hasn't returned yet

    runner.release.set()
    thread.join(timeout=5)
    assert result.get("done") is True


def test_wait_no_running_times_out_while_a_job_is_still_running() -> None:
    runner = _GatedRunner()  # never released within this test
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    queue.enqueue("/media/gate.mkv", DownmixSettings())
    assert runner.started.wait(timeout=5)

    assert queue.wait_no_running(timeout=0.2) is False

    runner.release.set()  # unblock so the pool can shut down cleanly
    queue.wait_idle(timeout=5)


def _write_global_settings(settings: Settings, **fields: object) -> None:
    """Persist arbitrary ``GlobalSettings`` fields into ``settings``' database.

    Runs the migration chain (the same schema ``from_settings`` will find)
    then writes the singleton row via :func:`update_global_settings`, so a
    subsequent ``JobQueue.from_settings`` reads exactly these values back.
    Shared by every test in this module that needs a real persisted
    ``GlobalSettings`` row rather than the raw ``JobQueue(...)`` constructor's
    plain keyword arguments.
    """
    upgrade_to_head(settings)
    engine = create_engine_from_settings(settings)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        update_global_settings(session, **fields)  # type: ignore[arg-type]
    engine.dispose()


def test_from_settings_reads_max_concurrency_from_global_settings(tmp_path: Path) -> None:
    # database_path must point somewhere writable: from_settings() defaults
    # history_recorder to a real one (COL-21) when none is passed, which
    # opens a real engine against it -- the bare Settings() default
    # (/config/collapsarr.db) isn't writable outside a container.
    settings = Settings(_env_file=None, database_path=str(tmp_path / "collapsarr.db"))
    _write_global_settings(settings, concurrency_limit=5)

    queue = JobQueue.from_settings(settings, pipeline_runner=_stub_runner(_SUCCESS))

    assert queue.max_concurrency == 5


def test_from_settings_defaults_to_one(tmp_path: Path) -> None:
    """No GlobalSettings row written yet -- from_settings creates it with its documented default."""
    settings = Settings(_env_file=None, database_path=str(tmp_path / "collapsarr.db"))

    queue = JobQueue.from_settings(settings, pipeline_runner=_stub_runner(_SUCCESS))

    assert queue.max_concurrency == 1


# ---------------------------------------------------------------------------
# Concurrency limiting.
# ---------------------------------------------------------------------------


def test_five_jobs_run_strictly_serially_with_concurrency_one(tmp_path: Path) -> None:
    """The definitive AC case: 5 jobs, concurrency 1 -> no overlap, in order."""
    events: list[tuple[int, str]] = []
    events_lock = threading.Lock()

    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        index = int(file_path.stem)
        with events_lock:
            events.append((index, "start"))
        time.sleep(0.02)
        with events_lock:
            events.append((index, "end"))
        return _SUCCESS

    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    jobs = [queue.enqueue(tmp_path / f"{i}.mkv", DownmixSettings()) for i in range(5)]

    queue.start()
    queue.wait_idle()

    # A single worker always claims the lowest-priority (= earliest-enqueued)
    # pending job, so every job's start/end pair is contiguous and jobs never
    # interleave -- guaranteed, not merely likely, regardless of the sleep.
    assert events == [(i, phase) for i in range(5) for phase in ("start", "end")]
    assert all(job.status is JobStatus.SUCCEEDED for job in jobs)


def test_at_most_max_concurrency_jobs_run_at_once(tmp_path: Path) -> None:
    """With concurrency 3 and 6 jobs, no more than 3 ever run simultaneously."""
    active = 0
    max_active_seen = 0
    state_lock = threading.Lock()

    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        nonlocal active, max_active_seen
        with state_lock:
            active += 1
            max_active_seen = max(max_active_seen, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1
        return _SUCCESS

    queue = JobQueue(max_concurrency=3, pipeline_runner=runner)
    for i in range(6):
        queue.enqueue(tmp_path / f"{i}.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert max_active_seen == 3  # real parallelism happened, up to the cap...
    assert active == 0  # ...and every job finished cleanly
    assert all(job.status is JobStatus.SUCCEEDED for job in queue.list_jobs())


def test_concurrency_two_lets_two_jobs_overlap_via_barrier_rendezvous(tmp_path: Path) -> None:
    """A stronger, non-timing-based proof: 2 jobs must rendezvous to proceed.

    If the queue only ever ran one job at a time, this barrier would never
    be met and the test would hang/timeout instead of passing.
    """
    barrier = threading.Barrier(2, timeout=5)

    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        barrier.wait()
        return _SUCCESS

    queue = JobQueue(max_concurrency=2, pipeline_runner=runner)
    queue.enqueue(tmp_path / "a.mkv", DownmixSettings())
    queue.enqueue(tmp_path / "b.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert all(job.status is JobStatus.SUCCEEDED for job in queue.list_jobs())


# ---------------------------------------------------------------------------
# Priority pull, reorder, and cancel (COL-164): reordering/cancelling
# not-yet-claimed work is meaningful up to the moment a worker claims it.
# ---------------------------------------------------------------------------


def test_bumped_job_is_claimed_before_earlier_enqueued_pending_jobs() -> None:
    """AC: bump_to_front makes a job the next one claimed, ahead of earlier ones."""
    runner = _GatedRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    queue.enqueue("/media/gate.mkv", DownmixSettings())  # worker claims + blocks here
    assert runner.started.wait(timeout=5)

    # Enqueued while the worker is parked on the gate -- all three stay pending.
    queue.enqueue("/media/a.mkv", DownmixSettings())
    queue.enqueue("/media/b.mkv", DownmixSettings())
    last = queue.enqueue("/media/c.mkv", DownmixSettings())

    assert queue.bump_to_front(last.id) is True

    runner.release.set()
    assert queue.wait_idle(timeout=5) is True

    # c jumps ahead of the earlier-enqueued a and b once the gate frees.
    assert runner.order == ["gate", "c", "a", "b"]


def test_bump_to_front_returns_false_for_an_unknown_job() -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    assert queue.bump_to_front(uuid4()) is False


def test_cancel_before_claim_prevents_the_job_from_ever_running() -> None:
    """AC: cancel removes a still-PENDING job -- it never runs, and leaves list_jobs()."""
    runner = _GatedRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    queue.enqueue("/media/gate.mkv", DownmixSettings())  # worker claims + blocks here
    assert runner.started.wait(timeout=5)

    victim = queue.enqueue("/media/victim.mkv", DownmixSettings())
    assert queue.cancel(victim.id) is True
    assert queue.get_job(victim.id) is None
    assert victim not in queue.list_jobs()

    runner.release.set()
    assert queue.wait_idle(timeout=5) is True

    assert runner.order == ["gate"]  # victim never ran


def test_cancel_after_claim_returns_false_and_the_job_still_completes() -> None:
    """AC: cancelling an already-claimed job returns False (not an error); it runs on."""
    runner = _GatedRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    job = queue.enqueue("/media/gate.mkv", DownmixSettings())
    assert runner.started.wait(timeout=5)  # a worker has already claimed + is running it

    assert queue.cancel(job.id) is False  # too late -- not an error

    runner.release.set()
    assert queue.wait_idle(timeout=5) is True

    assert job.status is JobStatus.SUCCEEDED  # no crash, no double-processing
    assert runner.order == ["gate"]


def test_cancel_returns_false_for_an_unknown_job() -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    assert queue.cancel(uuid4()) is False


# ---------------------------------------------------------------------------
# Hard-kill a RUNNING job (COL-192): cancel_running terminates the in-flight
# subprocess, the job fails out, and its worker slot frees for the next job.
# ---------------------------------------------------------------------------


class _FakeProcess:
    """A stand-in for the job's live subprocess, killable by the cancel handle.

    Shaped for :func:`~collapsarr.downmix.cancellation._terminate_process_tree`:
    ``poll`` reports "still running", ``pid`` is a non-existent one so the
    process-group lookup falls through to ``kill`` (there is no real group), and
    ``kill`` records the termination and unblocks the runner -- exactly what a
    real ffmpeg does when its ``subprocess`` call returns after being signalled.
    """

    def __init__(self) -> None:
        self.pid = 2_000_000_000  # no such process -> os.getpgid raises, forcing kill()
        self.killed = threading.Event()

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:
        self.killed.set()


class _HardKillRunner:
    """A pipeline_runner whose ``victim`` job registers a fake subprocess and blocks.

    The COL-192 counterpart of :class:`_GatedRunner`: the ``victim`` job
    attaches a :class:`_FakeProcess` to the ``cancel_handle`` the queue threads
    in, then blocks until that process is killed (mimicking ffmpeg's
    ``subprocess`` call returning on a signal), returning a FAILED result. Any
    other job runs straight to success, so a job enqueued behind the victim
    proves the worker slot freed.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.process = _FakeProcess()

    def __call__(
        self,
        file_path: Path,
        settings: DownmixSettings,
        *,
        cancel_handle: CancellationHandle | None = None,
        **_: object,
    ) -> PipelineResult:
        if file_path.stem == "victim":
            assert cancel_handle is not None, "queue must thread a cancel handle into a RUNNING job"
            cancel_handle.attach(self.process)
            self.started.set()
            assert self.process.killed.wait(timeout=5), "victim subprocess was never killed"
            cancel_handle.detach(self.process)
            return _FAILED
        return _SUCCESS


def test_cancel_running_hard_kills_the_subprocess_fails_the_job_and_frees_the_slot() -> None:
    """COL-192 AC: cancelling a RUNNING job kills its subprocess, fails it, frees the slot."""
    runner = _HardKillRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    victim = queue.enqueue("/media/victim.mkv", DownmixSettings())
    assert runner.started.wait(timeout=5)  # worker claimed victim; its subprocess is attached

    # A second job waits behind the single busy worker -- it can only run once
    # the victim's slot frees.
    nxt = queue.enqueue("/media/next.mkv", DownmixSettings())
    assert nxt in queue.list_jobs()  # queued behind the busy worker

    assert queue.cancel_running(victim.id) is True

    assert queue.wait_idle(timeout=5) is True

    # Subprocess termination: the registered process was killed.
    assert runner.process.killed.is_set()
    # Status transition: the hard-killed job is FAILED (no distinct CANCELLED status).
    assert victim.status is JobStatus.FAILED
    # Worker-slot release: the next pending job then ran to completion.
    assert nxt.status is JobStatus.SUCCEEDED


def test_cancel_running_returns_false_for_a_still_pending_job() -> None:
    """cancel_running is RUNNING-only: a not-yet-claimed job is left for cancel() to remove."""
    runner = _GatedRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    queue.enqueue("/media/gate.mkv", DownmixSettings())  # worker claims + blocks here
    assert runner.started.wait(timeout=5)
    pending = queue.enqueue("/media/pending.mkv", DownmixSettings())

    assert queue.cancel_running(pending.id) is False  # still PENDING -- not its job
    assert pending.status is JobStatus.PENDING

    runner.release.set()
    assert queue.wait_idle(timeout=5) is True


def test_cancel_running_returns_false_for_an_unknown_job() -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    assert queue.cancel_running(uuid4()) is False


def test_cancel_running_returns_false_for_a_terminal_job() -> None:
    """A job that already finished is "too late" -- cancel_running is a no-op (False)."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    queue.start()
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    assert queue.wait_idle(timeout=5) is True
    assert job.status is JobStatus.SUCCEEDED

    assert queue.cancel_running(job.id) is False


def test_shutdown_lets_an_in_flight_job_finish_then_stops_the_pool() -> None:
    runner = _GatedRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    job = queue.enqueue("/media/gate.mkv", DownmixSettings())
    assert runner.started.wait(timeout=5)

    runner.release.set()
    queue.shutdown()  # joins the worker: the in-flight gate job runs to completion

    assert job.status is JobStatus.SUCCEEDED
    # After shutdown, a further enqueue records the job but never runs it.
    stranded = queue.enqueue("/media/after.mkv", DownmixSettings())
    assert queue.wait_idle(timeout=1.0) is False  # it stays pending forever
    assert stranded.status is JobStatus.PENDING


def test_start_is_idempotent() -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    queue.start()
    queue.start()  # must not raise or spawn a second pool
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    assert queue.wait_idle(timeout=5) is True
    assert job.status is JobStatus.SUCCEEDED


def test_job_dataclass_is_importable_from_package_root() -> None:
    """Sanity check the public re-exports from collapsarr.jobs."""
    from collapsarr.jobs import Job as ReexportedJob
    from collapsarr.jobs import JobQueue as ReexportedJobQueue

    assert ReexportedJob is Job
    assert ReexportedJobQueue is JobQueue


# ---------------------------------------------------------------------------
# failure_notifier hook (COL-37): called only for a job that reached FAILED.
# ---------------------------------------------------------------------------


def test_failure_notifier_is_called_for_a_failed_job() -> None:
    notified: list[Job] = []
    queue = JobQueue(pipeline_runner=_stub_runner(_FAILED), failure_notifier=notified.append)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert notified == [job]
    assert job.status is JobStatus.FAILED


def test_failure_notifier_is_not_called_for_a_succeeded_job() -> None:
    notified: list[Job] = []
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS), failure_notifier=notified.append)
    queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert notified == []


def test_failure_notifier_is_called_when_the_runner_raises_unexpectedly() -> None:
    def raising_runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        raise RuntimeError("boom")

    notified: list[Job] = []
    queue = JobQueue(pipeline_runner=raising_runner, failure_notifier=notified.append)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert notified == [job]


def test_a_raising_failure_notifier_does_not_fail_the_job_or_the_worker() -> None:
    """AC: notification failures must never crash or fail the job itself."""

    def raising_notifier(job: Job) -> None:
        raise RuntimeError("webhook unreachable")

    queue = JobQueue(pipeline_runner=_stub_runner(_FAILED), failure_notifier=raising_notifier)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    assert queue.wait_idle(timeout=5) is True  # must not hang or crash the worker

    assert job.status is JobStatus.FAILED


def test_no_failure_notifier_configured_is_a_noop() -> None:
    """Default (no failure_notifier passed) behaves exactly as before COL-37."""
    queue = JobQueue(pipeline_runner=_stub_runner(_FAILED))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert job.status is JobStatus.FAILED


# ---------------------------------------------------------------------------
# plex_analyzer hook (COL-211): called only for a job that reached SUCCEEDED.
# ---------------------------------------------------------------------------


def test_plex_analyzer_is_called_for_a_succeeded_job() -> None:
    analyzed: list[Job] = []
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS), plex_analyzer=analyzed.append)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert analyzed == [job]
    assert job.status is JobStatus.SUCCEEDED


def test_plex_analyzer_is_not_called_for_a_failed_job() -> None:
    analyzed: list[Job] = []
    queue = JobQueue(pipeline_runner=_stub_runner(_FAILED), plex_analyzer=analyzed.append)
    queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert analyzed == []


def test_plex_analyzer_is_not_called_when_the_runner_raises_unexpectedly() -> None:
    """An unexpected runner exception fails the job -- FAILED, not SUCCEEDED -- so
    plex_analyzer (success-gated, unlike failure_notifier) must not fire for it."""

    def raising_runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        raise RuntimeError("boom")

    analyzed: list[Job] = []
    queue = JobQueue(pipeline_runner=raising_runner, plex_analyzer=analyzed.append)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert analyzed == []
    assert job.status is JobStatus.FAILED


def test_a_raising_plex_analyzer_does_not_fail_the_job_or_the_worker() -> None:
    """AC: a deliberately raising plex_analyzer must never fail the job or hang the queue."""

    def raising_analyzer(job: Job) -> None:
        raise RuntimeError("plex unreachable")

    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS), plex_analyzer=raising_analyzer)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    assert queue.wait_idle(timeout=5) is True  # must not hang or crash the worker

    assert job.status is JobStatus.SUCCEEDED


def test_no_plex_analyzer_configured_is_a_noop() -> None:
    """Default (no plex_analyzer passed) behaves exactly as before COL-211."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert job.status is JobStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Job lifecycle logging (COL-129): INFO on start/success, ERROR on a crash.
# ---------------------------------------------------------------------------


def test_run_job_logs_info_on_start_with_job_id_file_path_and_target(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    settings = DownmixSettings(enabled_targets=frozenset({DownmixTarget.FIVE_POINT_ONE}))

    with caplog.at_level(logging.INFO, logger="collapsarr"):
        job = queue.enqueue("/media/movie.mkv", settings)
        queue.start()
        queue.wait_idle()

    start_records = [
        r for r in caplog.records if r.levelno == logging.INFO and "started" in r.message
    ]
    assert len(start_records) == 1
    message = start_records[0].message
    assert str(job.id) in message
    assert "/media/movie.mkv" in message
    assert "5.1" in message


def test_run_job_logs_info_on_successful_completion(caplog: pytest.LogCaptureFixture) -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))

    with caplog.at_level(logging.INFO, logger="collapsarr"):
        job = queue.enqueue("/media/movie.mkv", DownmixSettings())
        queue.start()
        queue.wait_idle()

    completion_records = [
        r for r in caplog.records if r.levelno == logging.INFO and "completed" in r.message
    ]
    assert len(completion_records) == 1
    assert str(job.id) in completion_records[0].message
    assert job.status is JobStatus.SUCCEEDED


def test_run_job_does_not_log_completion_info_for_a_failed_job(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A job that fails via a returned PipelineResult isn't double-logged here --
    the pipeline itself already logs its own WARNING/ERROR for that outcome."""
    queue = JobQueue(pipeline_runner=_stub_runner(_FAILED))

    with caplog.at_level(logging.INFO, logger="collapsarr"):
        queue.enqueue("/media/movie.mkv", DownmixSettings())
        queue.start()
        queue.wait_idle()

    completion_records = [
        r for r in caplog.records if r.levelno == logging.INFO and "completed" in r.message
    ]
    assert completion_records == []


def test_run_job_logs_error_when_the_runner_raises_unexpectedly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def raising_runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        raise RuntimeError("boom")

    queue = JobQueue(pipeline_runner=raising_runner)

    with caplog.at_level(logging.INFO, logger="collapsarr"):
        job = queue.enqueue("/media/movie.mkv", DownmixSettings())
        queue.start()
        queue.wait_idle()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert str(job.id) in errors[0].message
    # logger.exception() carries the traceback via exc_info (so "boom" and the
    # RuntimeError show up in the formatted output/exc_text), not folded into
    # the plain message -- matching this repo's existing bare-except convention.
    assert errors[0].exc_info is not None
    assert errors[0].exc_info[1] is not None
    assert str(errors[0].exc_info[1]) == "boom"
    assert job.status is JobStatus.FAILED


# ---------------------------------------------------------------------------
# Default Audio Track preference wiring: the persisted opt-in toggle and
# (language, tier) preference must actually reach a real job dispatched
# through a JobQueue built via from_settings. Through COL-152, that meant
# run_downmix_pipeline's own kwargs (folded into the *same* remux); COL-251
# replaces that in-band fold with a separate, delayed SET_DEFAULT_AUDIO Job
# scheduled after a DOWNMIX job succeeds -- see the "Downmix-triggered
# delayed Default Audio Track Job" section further below for those tests.
# ---------------------------------------------------------------------------


class _KwargsCapturingRunner:
    """A pipeline_runner stub that records the **kwargs each job passes it."""

    def __init__(self, result: PipelineResult) -> None:
        self._result = result
        self.kwargs_calls: list[dict[str, object]] = []

    def __call__(
        self, file_path: Path, settings: DownmixSettings, **kwargs: object
    ) -> PipelineResult:
        self.kwargs_calls.append(kwargs)
        return self._result


def _write_default_audio_settings(
    settings: Settings,
    *,
    auto_set_default_audio: bool,
    default_audio_language: str | None = None,
    default_audio_channel_tier: DownmixTarget | None = None,
    default_audio_delay_minutes: int | None = None,
) -> None:
    """Persist the Default Audio Track settings into ``settings``' database.

    A thin, named wrapper around :func:`_write_global_settings` for this
    module's Default Audio Track tests (COL-152/COL-243/COL-251).
    """
    fields: dict[str, object] = {
        "default_audio_language": default_audio_language,
        "default_audio_channel_tier": default_audio_channel_tier,
        "auto_set_default_audio": auto_set_default_audio,
    }
    if default_audio_delay_minutes is not None:
        fields["default_audio_delay_minutes"] = default_audio_delay_minutes
    _write_global_settings(settings, **fields)


def test_from_settings_passes_no_default_audio_kwargs_to_downmix_jobs(tmp_path: Path) -> None:
    """A DOWNMIX job's pipeline_kwargs never carries a Default Audio Track key (COL-251):
    that fix is no longer folded in-band regardless of the toggle -- see
    ``_resolve_default_audio_auto_schedule`` instead."""
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "collapsarr.db"),
        data_dir=str(tmp_path),
    )
    _write_default_audio_settings(
        settings,
        auto_set_default_audio=True,
        default_audio_language="eng",
        default_audio_channel_tier=DownmixTarget.FIVE_POINT_ONE,
    )

    runner = _KwargsCapturingRunner(_SUCCESS)
    queue = JobQueue.from_settings(settings, pipeline_runner=runner)
    queue.enqueue("/media/movie.mkv", DownmixSettings())
    queue.start()
    queue.wait_idle()

    assert len(runner.kwargs_calls) == 1
    # The only per-call kwarg the queue threads to a DOWNMIX job is COL-192's
    # hard-kill handle (created per RUNNING job).
    assert set(runner.kwargs_calls[0]) == {"cancel_handle"}
    assert isinstance(runner.kwargs_calls[0]["cancel_handle"], CancellationHandle)


# ---------------------------------------------------------------------------
# ffmpeg_path override wiring (COL-218): a configured FFmpeg path must reach
# run_downmix_pipeline's ``ffmpeg_path`` kwarg for a real job dispatched
# through a JobQueue built via from_settings; unset must add nothing.
# ---------------------------------------------------------------------------


def _write_ffmpeg_path(settings: Settings, ffmpeg_path: str | None) -> None:
    """Persist ``ffmpeg_path`` directly on the singleton row.

    Not routed through :func:`update_global_settings` -- ``ffmpeg_path`` has
    no public setter there yet (COL-218 only wires the read path; a write
    path -- e.g. an opt-in UI -- is COL-222's concern, which COL-218
    deliberately blocks until this schema/wiring lands).
    """
    from collapsarr.settings.service import get_global_settings

    upgrade_to_head(settings)
    engine = create_engine_from_settings(settings)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        row = get_global_settings(session)
        row.ffmpeg_path = ffmpeg_path
        session.commit()
    engine.dispose()


def test_from_settings_threads_ffmpeg_path_when_set(tmp_path: Path) -> None:
    """A configured ffmpeg_path reaches the pipeline runner's kwargs."""
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "collapsarr.db"),
        data_dir=str(tmp_path),
    )
    _write_ffmpeg_path(settings, "/opt/collapsarr/ffmpeg/ffmpeg")

    runner = _KwargsCapturingRunner(_SUCCESS)
    queue = JobQueue.from_settings(settings, pipeline_runner=runner)
    queue.enqueue("/media/movie.mkv", DownmixSettings())
    queue.start()
    queue.wait_idle()

    assert len(runner.kwargs_calls) == 1
    assert runner.kwargs_calls[0]["ffmpeg_path"] == "/opt/collapsarr/ffmpeg/ffmpeg"


def test_from_settings_passes_no_ffmpeg_path_kwarg_when_unset(tmp_path: Path) -> None:
    """Unset (the default, every fresh/existing install's row): no ffmpeg_path kwarg added --
    a job's pipeline call is byte-for-byte what it was before COL-218."""
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "collapsarr.db"),
        data_dir=str(tmp_path),
    )
    upgrade_to_head(settings)

    runner = _KwargsCapturingRunner(_SUCCESS)
    queue = JobQueue.from_settings(settings, pipeline_runner=runner)
    queue.enqueue("/media/movie.mkv", DownmixSettings())
    queue.start()
    queue.wait_idle()

    assert len(runner.kwargs_calls) == 1
    kwargs = runner.kwargs_calls[0]
    assert "ffmpeg_path" not in kwargs
    assert set(kwargs) == {"cancel_handle"}


# ---------------------------------------------------------------------------
# Job kinds (COL-155): SET_DEFAULT_AUDIO jobs run through their own injected
# runner, sharing this queue's concurrency limit; DOWNMIX jobs are unaffected.
# ---------------------------------------------------------------------------

_PREFERENCE = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE)


class _StubDefaultAudioRunner:
    """A default_audio_pipeline_runner stub recording (file_path, preference) calls."""

    def __init__(self, result: PipelineResult) -> None:
        self._result = result
        self.calls: list[tuple[Path, DefaultAudioPreference]] = []

    def __call__(
        self, file_path: Path, preference: DefaultAudioPreference, **_: object
    ) -> PipelineResult:
        self.calls.append((file_path, preference))
        return self._result


def test_enqueue_default_audio_creates_a_pending_set_default_audio_job() -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))

    job = queue.enqueue_default_audio("/media/movie.mkv", _PREFERENCE)

    assert job.file_path == Path("/media/movie.mkv")
    assert job.kind is JobKind.SET_DEFAULT_AUDIO
    assert job.preference == _PREFERENCE
    assert job.status is JobStatus.PENDING
    assert queue.list_jobs() == [job]


def test_enqueue_creates_a_downmix_job_by_default() -> None:
    """Unchanged from before COL-155: a plain enqueue() is always a DOWNMIX job."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))

    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    assert job.kind is JobKind.DOWNMIX
    assert job.preference is None


def test_enqueue_default_audio_shares_the_same_priority_sequence_as_enqueue() -> None:
    """COL-163: a DOWNMIX and a SET_DEFAULT_AUDIO job interleave into one priority order."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))

    downmix_job = queue.enqueue("/media/a.mkv", DownmixSettings())
    default_audio_job = queue.enqueue_default_audio("/media/b.mkv", _PREFERENCE)
    another_downmix_job = queue.enqueue("/media/c.mkv", DownmixSettings())

    assert (downmix_job.priority, default_audio_job.priority, another_downmix_job.priority) == (
        0,
        1,
        2,
    )


def test_worker_pool_dispatches_set_default_audio_jobs_to_their_own_runner() -> None:
    downmix_runner = _stub_runner(_SUCCESS)
    default_audio_runner = _StubDefaultAudioRunner(_SUCCESS)
    queue = JobQueue(
        pipeline_runner=downmix_runner, default_audio_pipeline_runner=default_audio_runner
    )
    job = queue.enqueue_default_audio("/media/movie.mkv", _PREFERENCE)

    queue.start()
    queue.wait_idle()

    assert job.status is JobStatus.SUCCEEDED
    assert job.result is _SUCCESS
    assert default_audio_runner.calls == [(Path("/media/movie.mkv"), _PREFERENCE)]
    assert downmix_runner.calls == []  # the DOWNMIX runner is never touched


class _KwargsCapturingDefaultAudioRunner:
    """A default_audio_pipeline_runner stub that records the **kwargs each call passes it."""

    def __init__(self, result: PipelineResult) -> None:
        self._result = result
        self.kwargs_calls: list[dict[str, object]] = []

    def __call__(
        self, file_path: Path, preference: DefaultAudioPreference, **kwargs: object
    ) -> PipelineResult:
        self.kwargs_calls.append(kwargs)
        return self._result


def test_from_settings_threads_ffmpeg_path_to_set_default_audio_jobs_too(tmp_path: Path) -> None:
    """A configured ffmpeg_path must also reach a SET_DEFAULT_AUDIO job's runner
    (COL-218) -- not just a DOWNMIX job's, since run_default_audio_pipeline
    accepts the same ffmpeg_path kwarg as run_downmix_pipeline."""
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "collapsarr.db"),
        data_dir=str(tmp_path),
    )
    _write_ffmpeg_path(settings, "/opt/collapsarr/ffmpeg/ffmpeg")

    default_audio_runner = _KwargsCapturingDefaultAudioRunner(_SUCCESS)
    queue = JobQueue.from_settings(
        settings,
        pipeline_runner=_stub_runner(_SUCCESS),
        default_audio_pipeline_runner=default_audio_runner,
    )
    queue.enqueue_default_audio("/media/movie.mkv", _PREFERENCE)
    queue.start()
    queue.wait_idle()

    assert len(default_audio_runner.kwargs_calls) == 1
    assert (
        default_audio_runner.kwargs_calls[0]["ffmpeg_path"] == "/opt/collapsarr/ffmpeg/ffmpeg"
    )


class _StrictDefaultAudioRunner:
    """A default_audio_pipeline_runner stub matching run_default_audio_pipeline's
    exact kwarg surface -- ``ffmpeg_path``/``expected_stream_count`` only, no
    catch-all ``**kwargs`` -- so a call with any other keyword raises
    ``TypeError`` just like the real function would. Guards against a regression
    where :meth:`JobQueue._run_job` forwards an unsupported ``pipeline_kwargs``
    key to it (COL-218), or an unsupported per-Job kwarg (COL-251)."""

    def __init__(self, result: PipelineResult) -> None:
        self._result = result
        self.calls: list[tuple[Path, DefaultAudioPreference, str | None]] = []

    def __call__(
        self,
        file_path: Path,
        preference: DefaultAudioPreference,
        *,
        cancel_handle: object = None,
        ffmpeg_path: str | None = None,
        expected_stream_count: int | None = None,
    ) -> PipelineResult:
        self.calls.append((file_path, preference, ffmpeg_path))
        return self._result


def test_from_settings_does_not_forward_downmix_only_kwargs_to_set_default_audio_jobs(
    tmp_path: Path,
) -> None:
    """``pipeline_kwargs`` is a ``DOWNMIX``-job-only dict (COL-152's in-band fold moved
    out of it entirely -- COL-251); run_default_audio_pipeline has no catch-all
    **kwargs for arbitrary keys in it, so forwarding the whole dict unfiltered
    would raise TypeError on a real SET_DEFAULT_AUDIO job. ffmpeg_path, the one
    key both pipelines share, must still get through."""
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "collapsarr.db"),
        data_dir=str(tmp_path),
    )
    _write_default_audio_settings(
        settings,
        auto_set_default_audio=True,
        default_audio_language="eng",
        default_audio_channel_tier=DownmixTarget.FIVE_POINT_ONE,
    )
    _write_ffmpeg_path(settings, "/opt/collapsarr/ffmpeg/ffmpeg")

    default_audio_runner = _StrictDefaultAudioRunner(_SUCCESS)
    queue = JobQueue.from_settings(
        settings,
        pipeline_runner=_stub_runner(_SUCCESS),
        default_audio_pipeline_runner=default_audio_runner,
    )
    job = queue.enqueue_default_audio("/media/movie.mkv", _PREFERENCE)
    queue.start()
    queue.wait_idle()

    assert job.status is JobStatus.SUCCEEDED  # no TypeError from an unsupported kwarg
    assert default_audio_runner.calls == [
        (Path("/media/movie.mkv"), _PREFERENCE, "/opt/collapsarr/ffmpeg/ffmpeg")
    ]


def test_worker_pool_dispatches_downmix_jobs_to_the_downmix_runner_only() -> None:
    """The inverse: a DOWNMIX job never reaches the default_audio_pipeline_runner."""
    downmix_runner = _stub_runner(_SUCCESS)
    default_audio_runner = _StubDefaultAudioRunner(_SUCCESS)
    queue = JobQueue(
        pipeline_runner=downmix_runner, default_audio_pipeline_runner=default_audio_runner
    )
    queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert downmix_runner.calls == [(Path("/media/movie.mkv"), DownmixSettings())]
    assert default_audio_runner.calls == []


def test_worker_pool_captures_a_failed_set_default_audio_job() -> None:
    default_audio_runner = _StubDefaultAudioRunner(_FAILED)
    queue = JobQueue(default_audio_pipeline_runner=default_audio_runner)
    job = queue.enqueue_default_audio("/media/movie.mkv", _PREFERENCE)

    queue.start()
    queue.wait_idle()

    assert job.status is JobStatus.FAILED
    assert job.result is _FAILED


def test_default_audio_pipeline_runner_defaults_to_the_real_pipeline() -> None:
    """Mirrors pipeline_runner's own default -- a bare JobQueue() is production-ready."""
    queue = JobQueue()
    assert queue._default_audio_pipeline_runner is run_default_audio_pipeline


def test_downmix_and_set_default_audio_jobs_share_the_concurrency_cap(tmp_path: Path) -> None:
    """AC: the concurrency limit genuinely spans both kinds, not one pool each."""
    active = 0
    max_active_seen = 0
    state_lock = threading.Lock()

    def bump() -> None:
        nonlocal active, max_active_seen
        with state_lock:
            active += 1
            max_active_seen = max(max_active_seen, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1

    def downmix_runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        bump()
        return _SUCCESS

    def default_audio_runner(
        file_path: Path, preference: DefaultAudioPreference, **_: object
    ) -> PipelineResult:
        bump()
        return _SUCCESS

    queue = JobQueue(
        max_concurrency=2,
        pipeline_runner=downmix_runner,
        default_audio_pipeline_runner=default_audio_runner,
    )
    for i in range(3):
        queue.enqueue(tmp_path / f"downmix-{i}.mkv", DownmixSettings())
    for i in range(3):
        queue.enqueue_default_audio(tmp_path / f"default-audio-{i}.mkv", _PREFERENCE)

    queue.start()
    queue.wait_idle()

    assert max_active_seen == 2  # the cap held across both kinds combined
    assert all(job.status is JobStatus.SUCCEEDED for job in queue.list_jobs())


def test_from_settings_threads_default_audio_pipeline_runner_through(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, database_path=str(tmp_path / "collapsarr.db"), data_dir=str(tmp_path)
    )
    default_audio_runner = _StubDefaultAudioRunner(_SUCCESS)

    queue = JobQueue.from_settings(
        settings,
        pipeline_runner=_stub_runner(_SUCCESS),
        default_audio_pipeline_runner=default_audio_runner,
    )
    queue.enqueue_default_audio("/media/movie.mkv", _PREFERENCE)
    queue.start()
    queue.wait_idle()

    assert default_audio_runner.calls == [(Path("/media/movie.mkv"), _PREFERENCE)]


def test_run_job_start_log_reports_the_preference_for_a_set_default_audio_job(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue = JobQueue(default_audio_pipeline_runner=_StubDefaultAudioRunner(_SUCCESS))

    with caplog.at_level(logging.INFO, logger="collapsarr"):
        job = queue.enqueue_default_audio("/media/movie.mkv", _PREFERENCE)
        queue.start()
        queue.wait_idle()

    start_records = [
        r for r in caplog.records if r.levelno == logging.INFO and "started" in r.message
    ]
    assert len(start_records) == 1
    message = start_records[0].message
    assert str(job.id) in message
    assert "set_default_audio" in message
    assert "eng/5.1" in message


# ---------------------------------------------------------------------------
# Auto-Processing Pause (COL-226): a distinct, injected ``pause_check`` gate
# on the claim chokepoint itself -- direct queue-level tests, no DB. The
# real, Settings-backed wiring (``JobQueue.from_settings``) is covered
# separately below.
# ---------------------------------------------------------------------------


def test_no_pause_check_configured_claims_pending_jobs_as_normal() -> None:
    """AC: with no pause_check (the __init__ default), claiming is unaffected."""
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert job.status is JobStatus.SUCCEEDED
    assert runner.calls == [(Path("/media/movie.mkv"), DownmixSettings())]


def test_pause_check_returning_false_claims_pending_jobs_as_normal() -> None:
    """AC: a pause_check that always reports "not paused" behaves like no gate at all."""
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner, pause_check=lambda: False)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert job.status is JobStatus.SUCCEEDED


def test_pause_check_returning_true_blocks_a_pending_job_from_ever_being_claimed() -> None:
    """AC: while paused, a free worker never claims a new PENDING Job."""
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner, pause_check=lambda: True)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    # No notify_all ever fires to wake a waiter early while paused, so a short
    # real sleep is the only way to observe "nothing happened" -- long enough
    # to be well past any scheduling jitter, short relative to the test suite.
    time.sleep(0.2)

    assert job.status is JobStatus.PENDING
    assert runner.calls == []


def test_pause_check_toggling_off_lets_a_previously_pending_job_be_claimed() -> None:
    """AC: toggling the setting takes effect live -- no restart, no re-enqueue needed."""
    paused = {"value": True}
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner, pause_check=lambda: paused["value"])
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    time.sleep(0.2)
    assert job.status is JobStatus.PENDING  # still paused, nothing claimed yet

    paused["value"] = False
    # No event to wait on -- the worker only notices via its bounded poll
    # (_PAUSE_POLL_INTERVAL_SECONDS), so wait_idle's own timeout comfortably
    # covers the worst case.
    assert queue.wait_idle(timeout=5) is True

    assert queue.get_job(job.id) is not None
    assert queue.get_job(job.id).status is JobStatus.SUCCEEDED  # type: ignore[union-attr]


def test_pause_check_does_not_affect_an_already_running_job() -> None:
    """AC: a Job a worker has already claimed keeps running to completion while paused."""
    paused = {"value": False}
    runner = _GatedRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner, pause_check=lambda: paused["value"])
    queue.start()

    job = queue.enqueue("/media/gate.mkv", DownmixSettings())
    assert runner.started.wait(timeout=5)  # the sole worker has claimed and is running it

    paused["value"] = True  # pause kicks in only *after* the claim

    runner.release.set()
    assert queue.wait_idle(timeout=5) is True

    assert queue.get_job(job.id) is not None
    assert queue.get_job(job.id).status is JobStatus.SUCCEEDED  # type: ignore[union-attr]


def test_pause_check_is_consulted_on_every_claim_attempt() -> None:
    """The gate is re-evaluated per claim, not cached once -- a second job stays gated too."""
    paused = {"value": True}
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner, pause_check=lambda: paused["value"])
    first = queue.enqueue("/media/a.mkv", DownmixSettings())
    second = queue.enqueue("/media/b.mkv", DownmixSettings())

    queue.start()
    time.sleep(0.2)

    assert first.status is JobStatus.PENDING
    assert second.status is JobStatus.PENDING
    assert runner.calls == []


def test_from_settings_wires_a_real_pause_check_reading_auto_processing_paused(
    tmp_path: Path,
) -> None:
    """The production factory reads GlobalSettings.auto_processing_paused live (COL-226)."""
    settings = Settings(_env_file=None, database_path=str(tmp_path / "collapsarr.db"))
    _write_global_settings(settings, auto_processing_paused=True)
    runner = _stub_runner(_SUCCESS)

    queue = JobQueue.from_settings(settings, pipeline_runner=runner)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    queue.start()
    time.sleep(0.2)

    assert job.status is JobStatus.PENDING
    assert runner.calls == []


def test_from_settings_default_auto_processing_paused_claims_as_normal(tmp_path: Path) -> None:
    """No GlobalSettings row written -- from_settings creates it with its documented default."""
    settings = Settings(_env_file=None, database_path=str(tmp_path / "collapsarr.db"))
    runner = _stub_runner(_SUCCESS)

    queue = JobQueue.from_settings(settings, pipeline_runner=runner)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    queue.start()
    queue.wait_idle()

    assert job.status is JobStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Process Now (COL-229): force_start bypasses both Auto-Processing Pause and
# the Concurrency Limit -- direct queue-level tests, no DB.
# ---------------------------------------------------------------------------


def test_force_start_returns_false_for_an_unknown_job() -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    assert queue.force_start(uuid4()) is False


def test_force_start_returns_false_for_a_job_no_longer_pending() -> None:
    """AC ("too late"): a Job a worker already claimed can't be force-started again."""
    runner = _GatedRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    job = queue.enqueue("/media/gate.mkv", DownmixSettings())
    assert runner.started.wait(timeout=5)  # the sole worker has already claimed it

    assert queue.force_start(job.id) is False

    runner.release.set()
    assert queue.wait_idle(timeout=5) is True


def test_shutdown_waits_for_a_force_started_job_to_finish() -> None:
    """Regression: shutdown(wait=True) must join a force_start thread, not just the pool.

    force_start's dedicated thread isn't one of the fixed pool's ``_workers``
    -- shutdown must track and join it separately (``_force_start_threads``),
    or an orderly shutdown could return while a force-started job is still
    mid-run.
    """
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        started.set()
        assert release.wait(timeout=5)
        finished.set()
        return _SUCCESS

    queue = JobQueue(pipeline_runner=runner)  # pool never started -- force_start only
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    assert queue.force_start(job.id) is True
    assert started.wait(timeout=5)

    shutdown_thread = threading.Thread(target=lambda: queue.shutdown(wait=True, timeout=5))
    shutdown_thread.start()

    # shutdown() must block until the forced job's thread is joined -- give it
    # a moment, then prove it's still waiting before releasing the job.
    time.sleep(0.2)
    assert shutdown_thread.is_alive(), "shutdown() returned before the force-started job finished"
    assert not finished.is_set()

    release.set()
    shutdown_thread.join(timeout=5)
    assert not shutdown_thread.is_alive()
    assert finished.is_set()
    finished_job = queue.get_job(job.id)
    assert finished_job is not None
    assert finished_job.status is JobStatus.SUCCEEDED


def test_force_start_returns_false_once_shutdown_has_begun() -> None:
    """A force_start call racing (or following) shutdown() must not start new work."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.shutdown()

    assert queue.force_start(job.id) is False
    assert job.status is JobStatus.PENDING  # left exactly as it was, never ran


def test_force_start_flips_a_pending_job_to_running_synchronously() -> None:
    """The status flip happens under the lock, before the new thread even starts."""
    release = threading.Event()

    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        assert release.wait(timeout=5)
        return _SUCCESS

    queue = JobQueue(pipeline_runner=runner)  # never started -- no pool at all
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    assert job.status is JobStatus.PENDING

    assert queue.force_start(job.id) is True
    # True immediately, before the pipeline even runs -- a fresh lookup rather
    # than `job.status` again, since it's re-checked below against a
    # different terminal status once the pipeline actually completes.
    running = queue.get_job(job.id)
    assert running is not None
    assert running.status is JobStatus.RUNNING

    release.set()
    assert queue.wait_idle(timeout=5) is True
    finished = queue.get_job(job.id)
    assert finished is not None
    assert finished.status is JobStatus.SUCCEEDED


def test_force_start_runs_a_job_even_though_the_pool_was_never_started() -> None:
    """AC: Process Now doesn't depend on the fixed worker pool at all."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))  # .start() deliberately never called
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    assert queue.force_start(job.id) is True
    assert queue.wait_idle(timeout=5) is True

    assert job.status is JobStatus.SUCCEEDED


def test_force_start_bypasses_auto_processing_pause() -> None:
    """AC: Process Now works identically whether Auto-Processing Pause is on or off."""
    runner = _GatedRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner, pause_check=lambda: True)
    queue.start()

    job = queue.enqueue("/media/gate.mkv", DownmixSettings())
    time.sleep(0.2)
    assert job.status is JobStatus.PENDING  # the sole worker is paused, never claims it

    assert queue.force_start(job.id) is True
    assert runner.started.wait(timeout=5)  # force_start's own dedicated thread ran it anyway

    runner.release.set()
    assert queue.wait_idle(timeout=5) is True
    finished = queue.get_job(job.id)
    assert finished is not None
    assert finished.status is JobStatus.SUCCEEDED


def test_force_start_runs_alongside_an_already_running_pool_job_over_the_limit() -> None:
    """AC: Process Now genuinely exceeds the Concurrency Limit, not just reorders.

    A strong, non-timing-based proof (mirrors
    ``test_concurrency_two_lets_two_jobs_overlap_via_barrier_rendezvous``):
    with ``max_concurrency=1``, the sole pool worker claims the first job and
    blocks on the barrier; ``force_start`` runs the second job on its own
    dedicated thread. If force_start didn't genuinely run concurrently with
    the pool worker (e.g. it just waited for a pool slot to free), the
    barrier would never be met and this test would hang/timeout instead of
    passing.
    """
    barrier = threading.Barrier(2, timeout=5)

    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        barrier.wait()
        return _SUCCESS

    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    pool_job = queue.enqueue("/media/a.mkv", DownmixSettings())
    forced_job = queue.enqueue("/media/b.mkv", DownmixSettings())

    assert queue.force_start(forced_job.id) is True
    assert queue.wait_idle(timeout=5) is True

    assert pool_job.status is JobStatus.SUCCEEDED
    assert forced_job.status is JobStatus.SUCCEEDED
    # count_running observed 0 once idle -- both finished, neither stuck.
    assert queue.count_running() == 0


def test_force_started_job_still_updates_via_history_recorder_and_terminal_hook() -> None:
    """A force-started job is an ordinary run of ``_run_job`` -- its side effects still fire."""
    recorded: list[JobStatus] = []
    terminal_hook_calls: list[UUID] = []

    queue = JobQueue(
        pipeline_runner=_stub_runner(_SUCCESS),
        history_recorder=lambda job: recorded.append(job.status),
    )
    queue.set_job_terminal_hook(lambda job: terminal_hook_calls.append(job.id))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    assert queue.force_start(job.id) is True
    assert queue.wait_idle(timeout=5) is True

    assert recorded == [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.SUCCEEDED]
    assert terminal_hook_calls == [job.id]


def test_count_running_counts_only_running_jobs() -> None:
    runner = _GatedRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    assert queue.count_running() == 0

    gate_job = queue.enqueue("/media/gate.mkv", DownmixSettings())
    assert runner.started.wait(timeout=5)
    queue.enqueue("/media/pending.mkv", DownmixSettings())  # stays PENDING behind the gate

    assert queue.count_running() == 1

    runner.release.set()
    assert queue.wait_idle(timeout=5) is True
    assert queue.count_running() == 0
    assert gate_job.status is JobStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Scheduled Job due-time gate (COL-242): an injectable ``now`` clock plus a
# nullable ``Job.scheduled_at`` -- a still-PENDING Job isn't claimed by the
# ordinary worker-pool claim path until its scheduled_at has passed (or is
# unset, the default -- every existing Job kind/trigger is unaffected).
# ---------------------------------------------------------------------------


def test_job_scheduled_at_defaults_to_none() -> None:
    """AC: every existing Job kind/trigger is unaffected -- scheduled_at is unset by default."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    assert job.scheduled_at is None


def test_pending_job_with_future_scheduled_at_is_not_claimed_by_the_worker_pool() -> None:
    """AC: a pending Job whose scheduled_at is still in the future is not claimed."""
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner, now=lambda: _FIXED_NOW)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    job.scheduled_at = _FIXED_NOW + timedelta(hours=1)

    queue.start()
    time.sleep(0.2)

    assert job.status is JobStatus.PENDING
    assert runner.calls == []


def test_pending_job_with_past_scheduled_at_is_claimed_as_today() -> None:
    """AC: a pending Job whose scheduled_at has already passed is claimed as today."""
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner, now=lambda: _FIXED_NOW)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    job.scheduled_at = _FIXED_NOW - timedelta(hours=1)

    queue.start()
    assert queue.wait_idle(timeout=5) is True

    assert job.status is JobStatus.SUCCEEDED
    assert len(runner.calls) == 1


def test_pending_job_with_scheduled_at_exactly_now_is_claimed() -> None:
    """The due-time comparison is inclusive: scheduled_at == now is already due."""
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner, now=lambda: _FIXED_NOW)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    job.scheduled_at = _FIXED_NOW

    queue.start()
    assert queue.wait_idle(timeout=5) is True

    assert job.status is JobStatus.SUCCEEDED


def test_a_future_scheduled_job_never_blocks_an_earlier_priority_claimable_job() -> None:
    """A future-scheduled Job is skipped entirely -- an unrelated claimable Job still runs.

    Doesn't use ``wait_idle`` here: with ``now`` fixed, ``scheduled_job`` stays
    genuinely ``PENDING`` forever (correctly -- it hasn't run), so a queue
    that's fully idle in the ordinary sense never actually happens. Polls
    ``claimable_job`` directly instead.
    """
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner, now=lambda: _FIXED_NOW)
    scheduled_job = queue.enqueue("/media/later.mkv", DownmixSettings())
    scheduled_job.scheduled_at = _FIXED_NOW + timedelta(hours=1)
    claimable_job = queue.enqueue("/media/now.mkv", DownmixSettings())

    queue.start()
    deadline = time.monotonic() + 5
    while claimable_job.status is JobStatus.PENDING and time.monotonic() < deadline:
        time.sleep(0.05)

    assert claimable_job.status is JobStatus.SUCCEEDED
    assert scheduled_job.status is JobStatus.PENDING


def test_worker_pool_claims_a_scheduled_job_once_its_due_time_passes() -> None:
    """AC: the gate is live, not just checked once -- toggling the clock forward wakes a worker.

    Mirrors ``test_pause_check_toggling_off_lets_a_previously_pending_job_be_claimed``:
    nothing explicitly wakes a worker blocked purely on a future scheduled_at
    (see ``_SCHEDULE_POLL_INTERVAL_SECONDS``), so this proves the bounded poll
    actually notices the due time passing, not just the gate's initial check.
    """
    current_now = {"value": _FIXED_NOW}
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner, now=lambda: current_now["value"])
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    job.scheduled_at = _FIXED_NOW + timedelta(seconds=1)

    queue.start()
    time.sleep(0.2)
    assert job.status is JobStatus.PENDING  # not due yet

    current_now["value"] = _FIXED_NOW + timedelta(hours=1)  # advance past the due time
    # No event to wait on -- the worker only notices via its bounded poll
    # (_SCHEDULE_POLL_INTERVAL_SECONDS), so wait_idle's own timeout comfortably
    # covers the worst case.
    assert queue.wait_idle(timeout=5) is True

    assert queue.get_job(job.id) is not None
    assert queue.get_job(job.id).status is JobStatus.SUCCEEDED  # type: ignore[union-attr]


def test_force_start_bypasses_the_scheduled_at_gate() -> None:
    """AC: Process Now still claims a pending Job regardless of scheduled_at."""
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner, now=lambda: _FIXED_NOW)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    job.scheduled_at = _FIXED_NOW + timedelta(days=1)

    queue.start()
    time.sleep(0.2)
    assert job.status is JobStatus.PENDING  # the pool never claims it

    assert queue.force_start(job.id) is True
    assert queue.wait_idle(timeout=5) is True

    assert queue.get_job(job.id) is not None
    assert queue.get_job(job.id).status is JobStatus.SUCCEEDED  # type: ignore[union-attr]


def test_from_settings_defaults_now_to_a_real_clock(tmp_path: Path) -> None:
    """The production factory's default `now` genuinely reflects wall-clock time."""
    settings = Settings(_env_file=None, database_path=str(tmp_path / "collapsarr.db"))
    runner = _stub_runner(_SUCCESS)

    queue = JobQueue.from_settings(settings, pipeline_runner=runner)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    job.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)  # already due
    queue.start()
    assert queue.wait_idle(timeout=5) is True

    assert job.status is JobStatus.SUCCEEDED


def test_from_settings_accepts_an_injected_now_clock(tmp_path: Path) -> None:
    """AC-adjacent: `from_settings` threads an explicit `now` through, same as the raw ctor."""
    settings = Settings(_env_file=None, database_path=str(tmp_path / "collapsarr.db"))
    runner = _stub_runner(_SUCCESS)

    queue = JobQueue.from_settings(settings, pipeline_runner=runner, now=lambda: _FIXED_NOW)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    job.scheduled_at = _FIXED_NOW + timedelta(hours=1)

    queue.start()
    time.sleep(0.2)

    assert job.status is JobStatus.PENDING
    assert runner.calls == []


# ---------------------------------------------------------------------------
# Downmix-triggered delayed Default Audio Track Job (COL-251): a DOWNMIX job
# that actually adds a track schedules a follow-up SET_DEFAULT_AUDIO Job,
# using COL-242's due-time gate/injectable clock and COL-243's delay setting.
# ---------------------------------------------------------------------------

_DOWNMIX_TARGET = QualifyingTarget(language="eng", target=DownmixTarget.FIVE_POINT_ONE)
_SUCCESS_WITH_NEW_STREAM = PipelineResult(
    outcome=PipelineOutcome.SUCCESS,
    success=True,
    detail="ok",
    tracks_added=(_DOWNMIX_TARGET,),
    final_stream_count=3,
)
_DEFAULT_AUDIO_PREFERENCE = DefaultAudioPreference(
    language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE
)


def _wait_for_job_count(queue: JobQueue, count: int, *, timeout: float = 5) -> None:
    """Poll until ``queue`` has enqueued ``count`` Jobs, or ``timeout`` elapses.

    Deliberately not :meth:`JobQueue.wait_idle`: a downmix-triggered Job's
    ``scheduled_at`` may never come due against a fixed injected ``now``, so
    the queue never becomes idle in the ordinary sense -- mirroring
    ``test_a_future_scheduled_job_never_blocks_an_earlier_priority_claimable_job``
    above.
    """
    deadline = time.monotonic() + timeout
    while len(queue.list_jobs()) < count and time.monotonic() < deadline:
        time.sleep(0.05)


def test_downmix_job_that_adds_a_track_schedules_a_default_audio_job() -> None:
    """AC: scheduled_at = now + delay, expected_stream_count = the downmix's final count."""
    queue = JobQueue(
        pipeline_runner=_stub_runner(_SUCCESS_WITH_NEW_STREAM),
        now=lambda: _FIXED_NOW,
        default_audio_auto_schedule=DefaultAudioAutoSchedule(
            preference=_DEFAULT_AUDIO_PREFERENCE, delay=timedelta(minutes=45)
        ),
    )
    downmix_job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    _wait_for_job_count(queue, 2)

    assert downmix_job.status is JobStatus.SUCCEEDED
    jobs = queue.list_jobs()
    assert len(jobs) == 2
    scheduled_job = next(job for job in jobs if job.kind is JobKind.SET_DEFAULT_AUDIO)
    assert scheduled_job.file_path == downmix_job.file_path
    assert scheduled_job.preference == _DEFAULT_AUDIO_PREFERENCE
    assert scheduled_job.scheduled_at == _FIXED_NOW + timedelta(minutes=45)
    assert scheduled_job.expected_stream_count == 3
    # now is fixed at _FIXED_NOW, so the due time never arrives in this test.
    assert scheduled_job.status is JobStatus.PENDING


def test_downmix_job_with_nothing_to_do_schedules_nothing() -> None:
    """A downmix that adds no track (NOTHING_TO_DO) has nothing new for Plex to ingest."""
    queue = JobQueue(
        pipeline_runner=_stub_runner(_NOTHING_TO_DO),
        now=lambda: _FIXED_NOW,
        default_audio_auto_schedule=DefaultAudioAutoSchedule(
            preference=_DEFAULT_AUDIO_PREFERENCE, delay=timedelta(minutes=45)
        ),
    )
    downmix_job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    assert queue.wait_idle(timeout=5) is True

    assert downmix_job.status is JobStatus.SUCCEEDED
    assert len(queue.list_jobs()) == 1


def test_downmix_job_that_fails_schedules_nothing() -> None:
    """A FAILED downmix never schedules a follow-up -- nothing succeeded to fix disposition on."""
    queue = JobQueue(
        pipeline_runner=_stub_runner(_FAILED),
        now=lambda: _FIXED_NOW,
        default_audio_auto_schedule=DefaultAudioAutoSchedule(
            preference=_DEFAULT_AUDIO_PREFERENCE, delay=timedelta(minutes=45)
        ),
    )
    downmix_job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    assert queue.wait_idle(timeout=5) is True

    assert downmix_job.status is JobStatus.FAILED
    assert len(queue.list_jobs()) == 1


def test_default_audio_auto_schedule_none_schedules_nothing() -> None:
    """The default (``default_audio_auto_schedule=None``): pre-COL-251 behaviour, unaffected."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS_WITH_NEW_STREAM), now=lambda: _FIXED_NOW)
    downmix_job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    assert queue.wait_idle(timeout=5) is True

    assert downmix_job.status is JobStatus.SUCCEEDED
    assert len(queue.list_jobs()) == 1


def test_set_default_audio_job_never_chains_another_schedule() -> None:
    """A SET_DEFAULT_AUDIO job's own success never schedules a further Job -- no chaining."""
    queue = JobQueue(
        default_audio_pipeline_runner=_StubDefaultAudioRunner(_SUCCESS),
        now=lambda: _FIXED_NOW,
        default_audio_auto_schedule=DefaultAudioAutoSchedule(
            preference=_DEFAULT_AUDIO_PREFERENCE, delay=timedelta(minutes=45)
        ),
    )
    job = queue.enqueue_default_audio("/media/movie.mkv", _DEFAULT_AUDIO_PREFERENCE)

    queue.start()
    assert queue.wait_idle(timeout=5) is True

    assert job.status is JobStatus.SUCCEEDED
    assert len(queue.list_jobs()) == 1


def test_from_settings_schedules_a_default_audio_job_when_toggle_on(tmp_path: Path) -> None:
    """AC: wired end-to-end through the real production factory."""
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "collapsarr.db"),
        data_dir=str(tmp_path),
    )
    _write_default_audio_settings(
        settings,
        auto_set_default_audio=True,
        default_audio_language="eng",
        default_audio_channel_tier=DownmixTarget.FIVE_POINT_ONE,
        default_audio_delay_minutes=45,
    )

    queue = JobQueue.from_settings(
        settings,
        pipeline_runner=_stub_runner(_SUCCESS_WITH_NEW_STREAM),
        now=lambda: _FIXED_NOW,
    )
    downmix_job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    _wait_for_job_count(queue, 2)

    assert downmix_job.status is JobStatus.SUCCEEDED
    jobs = queue.list_jobs()
    assert len(jobs) == 2
    scheduled_job = next(job for job in jobs if job.kind is JobKind.SET_DEFAULT_AUDIO)
    assert scheduled_job.scheduled_at == _FIXED_NOW + timedelta(minutes=45)
    assert scheduled_job.expected_stream_count == 3
    # Built via the real `as_default_audio_preference` adapter (COL-250), so it
    # also carries the persisted `ignore_commentary_tracks` column's own
    # default (True) -- unlike `_DEFAULT_AUDIO_PREFERENCE`, which every other
    # test in this module constructs directly and passes straight back in.
    assert scheduled_job.preference == DefaultAudioPreference(
        language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE, ignore_commentary_tracks=True
    )


def test_from_settings_schedules_nothing_when_toggle_off(tmp_path: Path) -> None:
    """AC: the manual/bulk trigger and plain webhook trigger are unaffected by the toggle being
    off -- and so is a downmix, which schedules nothing at all in that case."""
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "collapsarr.db"),
        data_dir=str(tmp_path),
    )
    _write_default_audio_settings(
        settings,
        auto_set_default_audio=False,
        default_audio_language="eng",
        default_audio_channel_tier=DownmixTarget.FIVE_POINT_ONE,
    )

    queue = JobQueue.from_settings(
        settings,
        pipeline_runner=_stub_runner(_SUCCESS_WITH_NEW_STREAM),
        now=lambda: _FIXED_NOW,
    )
    downmix_job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    assert queue.wait_idle(timeout=5) is True

    assert downmix_job.status is JobStatus.SUCCEEDED
    assert len(queue.list_jobs()) == 1


def test_from_settings_schedules_nothing_with_an_incomplete_preference(tmp_path: Path) -> None:
    """Toggle on but only the language half configured: no actionable preference, no schedule."""
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "collapsarr.db"),
        data_dir=str(tmp_path),
    )
    _write_default_audio_settings(
        settings,
        auto_set_default_audio=True,
        default_audio_language="eng",
        default_audio_channel_tier=None,
    )

    queue = JobQueue.from_settings(
        settings,
        pipeline_runner=_stub_runner(_SUCCESS_WITH_NEW_STREAM),
        now=lambda: _FIXED_NOW,
    )
    downmix_job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.start()
    assert queue.wait_idle(timeout=5) is True

    assert downmix_job.status is JobStatus.SUCCEEDED
    assert len(queue.list_jobs()) == 1


def test_manual_trigger_enqueue_default_audio_still_enqueues_immediately() -> None:
    """AC: the manual/bulk trigger (enqueue_default_audio with no scheduled_at) is unaffected --
    still enqueues immediately, with no due-time gate."""
    queue = JobQueue(default_audio_pipeline_runner=_StubDefaultAudioRunner(_SUCCESS))
    job = queue.enqueue_default_audio("/media/movie.mkv", _DEFAULT_AUDIO_PREFERENCE)

    assert job.scheduled_at is None
    assert job.expected_stream_count is None
