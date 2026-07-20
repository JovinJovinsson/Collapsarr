"""Tests for the job queue and bounded-concurrency worker pool (COL-20).

Every test drives :class:`~collapsarr.jobs.queue.JobQueue` with an injected
``pipeline_runner`` stub -- never the real ``ffmpeg``/``ffprobe``-backed
:func:`~collapsarr.downmix.pipeline.run_downmix_pipeline` -- since this
module's job is concurrency control and outcome capture, not the pipeline
itself (that's COL-19's, already covered by ``test_downmix_pipeline.py``).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from collapsarr.config import Settings
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.jobs.queue import DEFAULT_MAX_CONCURRENCY, Job, JobQueue, JobStatus

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


def test_default_max_concurrency_is_one() -> None:
    assert DEFAULT_MAX_CONCURRENCY == 1
    assert JobQueue().max_concurrency == 1


def test_rejects_a_non_positive_max_concurrency() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        JobQueue(max_concurrency=0)


# ---------------------------------------------------------------------------
# Execution: each job invokes the pipeline and captures its outcome.
# ---------------------------------------------------------------------------


def test_run_pending_invokes_the_pipeline_with_the_jobs_file_and_settings() -> None:
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(pipeline_runner=runner)
    settings = DownmixSettings()
    queue.enqueue("/media/movie.mkv", settings)

    jobs = queue.run_pending()

    assert len(jobs) == 1
    assert runner.calls == [(Path("/media/movie.mkv"), settings)]


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        (_SUCCESS, JobStatus.SUCCEEDED),
        (_NOTHING_TO_DO, JobStatus.SUCCEEDED),
        (_FAILED, JobStatus.FAILED),
    ],
)
def test_run_pending_captures_the_pipeline_outcome_onto_the_job(
    result: PipelineResult, expected_status: JobStatus
) -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(result))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    (ran_job,) = queue.run_pending()

    assert ran_job is job
    assert job.status is expected_status
    assert job.result is result
    assert job.error is None


def test_run_pending_captures_an_unexpected_runner_exception_as_a_failed_job() -> None:
    def raising_runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        raise RuntimeError("boom")

    queue = JobQueue(pipeline_runner=raising_runner)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.run_pending()

    assert job.status is JobStatus.FAILED
    assert job.result is None
    assert isinstance(job.error, RuntimeError)
    assert str(job.error) == "boom"


def test_run_pending_returns_empty_list_and_is_a_noop_when_nothing_pending() -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))

    assert queue.run_pending() == []


def test_run_pending_only_runs_jobs_pending_at_call_time() -> None:
    """A job enqueued after run_pending() starts isn't swept into that batch."""
    runner = _stub_runner(_SUCCESS)
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.enqueue("/media/a.mkv", DownmixSettings())

    first_batch = queue.run_pending()
    assert len(first_batch) == 1

    queue.enqueue("/media/b.mkv", DownmixSettings())
    second_batch = queue.run_pending()
    assert len(second_batch) == 1
    assert second_batch[0] is not first_batch[0]

    assert len(queue.list_jobs()) == 2
    assert all(job.status is JobStatus.SUCCEEDED for job in queue.list_jobs())


def test_from_settings_reads_max_concurrency_from_settings(tmp_path: Path) -> None:
    # database_path must point somewhere writable: from_settings() defaults
    # history_recorder to a real one (COL-21) when none is passed, which
    # opens a real engine against it -- the bare Settings() default
    # (/config/collapsarr.db) isn't writable outside a container.
    settings = Settings(
        _env_file=None, job_max_concurrency=5, database_path=str(tmp_path / "collapsarr.db")
    )

    queue = JobQueue.from_settings(settings, pipeline_runner=_stub_runner(_SUCCESS))

    assert queue.max_concurrency == 5


def test_from_settings_defaults_to_one(tmp_path: Path) -> None:
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

    ran = queue.run_pending()

    # A single worker drains its task queue strictly in submission order, so
    # every job's start/end pair is contiguous and jobs never interleave --
    # this is guaranteed, not merely likely, regardless of the sleep above.
    assert events == [(i, phase) for i in range(5) for phase in ("start", "end")]
    assert ran == jobs
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

    jobs = queue.run_pending()

    assert max_active_seen == 3  # real parallelism happened, up to the cap...
    assert active == 0  # ...and every job finished cleanly
    assert all(job.status is JobStatus.SUCCEEDED for job in jobs)


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

    jobs = queue.run_pending()

    assert all(job.status is JobStatus.SUCCEEDED for job in jobs)


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

    queue.run_pending()

    assert notified == [job]
    assert job.status is JobStatus.FAILED


def test_failure_notifier_is_not_called_for_a_succeeded_job() -> None:
    notified: list[Job] = []
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS), failure_notifier=notified.append)
    queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.run_pending()

    assert notified == []


def test_failure_notifier_is_called_when_the_runner_raises_unexpectedly() -> None:
    def raising_runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        raise RuntimeError("boom")

    notified: list[Job] = []
    queue = JobQueue(pipeline_runner=raising_runner, failure_notifier=notified.append)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.run_pending()

    assert notified == [job]


def test_a_raising_failure_notifier_does_not_fail_the_job_or_run_pending() -> None:
    """AC: notification failures must never crash or fail the job itself."""

    def raising_notifier(job: Job) -> None:
        raise RuntimeError("webhook unreachable")

    queue = JobQueue(pipeline_runner=_stub_runner(_FAILED), failure_notifier=raising_notifier)
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    jobs = queue.run_pending()  # must not raise

    assert jobs == [job]
    assert job.status is JobStatus.FAILED


def test_no_failure_notifier_configured_is_a_noop() -> None:
    """Default (no failure_notifier passed) behaves exactly as before COL-37."""
    queue = JobQueue(pipeline_runner=_stub_runner(_FAILED))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.run_pending()

    assert job.status is JobStatus.FAILED
