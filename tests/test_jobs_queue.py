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
from pathlib import Path
from uuid import uuid4

import pytest

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.downmix.cancellation import CancellationHandle
from collapsarr.downmix.default_audio import DefaultAudioPreference
from collapsarr.downmix.default_audio_pipeline import run_default_audio_pipeline
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.jobs.queue import DEFAULT_MAX_CONCURRENCY, Job, JobKind, JobQueue, JobStatus
from collapsarr.migrations import upgrade_to_head
from collapsarr.settings.service import update_global_settings

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
# Default Audio Track preference wiring (COL-152): the persisted opt-in toggle
# and (language, tier) preference must actually reach run_downmix_pipeline's
# kwargs for a real job dispatched through a JobQueue built via from_settings.
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
) -> None:
    """Persist the Default Audio Track settings into ``settings``' database.

    A thin, named wrapper around :func:`_write_global_settings` for this
    module's Default Audio Track tests (COL-152).
    """
    _write_global_settings(
        settings,
        default_audio_language=default_audio_language,
        default_audio_channel_tier=default_audio_channel_tier,
        auto_set_default_audio=auto_set_default_audio,
    )


def test_from_settings_threads_default_audio_preference_when_toggle_on(tmp_path: Path) -> None:
    """Toggle on: a job dispatched through the queue reaches the pipeline with the fix armed."""
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
    kwargs = runner.kwargs_calls[0]
    assert kwargs["auto_set_default_audio"] is True
    assert kwargs["default_audio_preference"] == DefaultAudioPreference(
        language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE
    )


def test_from_settings_passes_no_default_audio_kwargs_when_toggle_off(tmp_path: Path) -> None:
    """Toggle off (the default): no Default Audio fix kwargs -- only COL-192's cancel_handle."""
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "collapsarr.db"),
        data_dir=str(tmp_path),
    )
    # Even with a language/tier persisted, the off toggle must gate them out.
    _write_default_audio_settings(
        settings,
        auto_set_default_audio=False,
        default_audio_language="eng",
        default_audio_channel_tier=DownmixTarget.FIVE_POINT_ONE,
    )

    runner = _KwargsCapturingRunner(_SUCCESS)
    queue = JobQueue.from_settings(settings, pipeline_runner=runner)
    queue.enqueue("/media/movie.mkv", DownmixSettings())
    queue.start()
    queue.wait_idle()

    assert len(runner.kwargs_calls) == 1
    kwargs = runner.kwargs_calls[0]
    assert "auto_set_default_audio" not in kwargs
    assert "default_audio_preference" not in kwargs
    # The only per-call kwarg the queue now threads is COL-192's hard-kill
    # handle (created per RUNNING job); the Default Audio fix stays gated out.
    assert set(kwargs) == {"cancel_handle"}
    assert isinstance(kwargs["cancel_handle"], CancellationHandle)


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
