"""Tests for restart-durable queue rehydration (COL-166).

Every test seeds :class:`~collapsarr.jobs.models.JobHistory` rows directly
against the ``session`` fixture's schema-initialised DB (rather than driving
a real :class:`~collapsarr.jobs.queue.JobQueue` through ``enqueue`` first),
so each row's ``priority``/``status``/``kind``/``target``/``language`` are
fully under the test's control -- exactly the "DB seeded with several
PENDING rows at different priorities" shape the acceptance criteria ask for.
"""

from __future__ import annotations

import threading
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from collapsarr.downmix.default_audio import DefaultAudioPreference
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.jobs.models import JobHistory
from collapsarr.jobs.queue import Job, JobKind, JobQueue, JobStatus
from collapsarr.jobs.rehydrate import rehydrate_pending_jobs
from collapsarr.settings.service import update_global_settings

_SUCCESS = PipelineResult(outcome=PipelineOutcome.SUCCESS, success=True, detail="ok")


def _seed_history(
    session: Session,
    *,
    file_path: str,
    priority: int,
    status: JobStatus = JobStatus.PENDING,
    kind: JobKind = JobKind.DOWNMIX,
    job_id: str | None = None,
    target: str | None = "stereo",
    language: str | None = None,
) -> JobHistory:
    """Write one ``JobHistory`` row directly, bypassing ``record_job_history``.

    Gives each test full control over exactly what's on disk -- including a
    deliberately stale ``target``/``language`` summary, or a malformed
    ``job_id`` -- independent of whatever the *current* live
    ``GlobalSettings`` says, which is the whole point of the "config changed
    since the row was written" tests below.
    """
    row = JobHistory(
        job_id=job_id or str(uuid4()),
        file_path=file_path,
        status=status,
        kind=kind,
        priority=priority,
        target=target,
        language=language,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


class _RecordingRunner:
    """A pipeline_runner stub recording the order files were actually run in."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        with self._lock:
            self.order.append(file_path.stem)
        return _SUCCESS


# ---------------------------------------------------------------------------
# Reconstructing PENDING rows as live Jobs.
# ---------------------------------------------------------------------------


def test_rehydrate_reconstructs_every_pending_row_as_a_live_pending_job(
    session: Session,
) -> None:
    _seed_history(session, file_path="/media/a.mkv", priority=0)
    _seed_history(session, file_path="/media/b.mkv", priority=1)
    queue = JobQueue(pipeline_runner=_RecordingRunner())

    jobs = rehydrate_pending_jobs(session, queue)

    assert len(jobs) == 2
    assert {job.file_path for job in jobs} == {Path("/media/a.mkv"), Path("/media/b.mkv")}
    assert all(job.status is JobStatus.PENDING for job in jobs)
    assert {job.id for job in jobs} == {job.id for job in queue.list_jobs()}


def test_rehydrate_ignores_terminal_and_running_rows(session: Session) -> None:
    _seed_history(session, file_path="/media/done.mkv", priority=0, status=JobStatus.SUCCEEDED)
    _seed_history(session, file_path="/media/failed.mkv", priority=1, status=JobStatus.FAILED)
    _seed_history(session, file_path="/media/running.mkv", priority=2, status=JobStatus.RUNNING)
    queue = JobQueue(pipeline_runner=_RecordingRunner())

    jobs = rehydrate_pending_jobs(session, queue)

    assert jobs == []
    assert queue.list_jobs() == []


def test_rehydrated_job_keeps_the_history_rows_job_id(session: Session) -> None:
    """So the next record_job_history call updates the same row, not a duplicate."""
    row = _seed_history(session, file_path="/media/a.mkv", priority=0)
    queue = JobQueue(pipeline_runner=_RecordingRunner())

    (job,) = rehydrate_pending_jobs(session, queue)

    assert str(job.id) == row.job_id


def test_rehydrate_skips_a_row_with_a_malformed_job_id_without_raising(
    session: Session,
) -> None:
    _seed_history(session, file_path="/media/bad.mkv", priority=0, job_id="not-a-uuid")
    _seed_history(session, file_path="/media/good.mkv", priority=1)
    queue = JobQueue(pipeline_runner=_RecordingRunner())

    jobs = rehydrate_pending_jobs(session, queue)

    assert len(jobs) == 1
    assert jobs[0].file_path == Path("/media/good.mkv")


def test_a_row_that_cannot_be_rehydrated_is_marked_failed_not_left_a_ghost(
    session: Session,
) -> None:
    """AC: a PENDING row no longer sits forever with no backing live Job.

    An un-rehydratable row (malformed job_id here) can't get a live Job, but
    it must not stay silently PENDING forever either -- that's exactly the
    permanent "ghost pending row" this ticket exists to eliminate. It's
    flipped to FAILED, with error_text explaining why, so it's visible and
    the row leaves PENDING for good (not re-attempted-and-reskipped on every
    future restart).
    """
    row = _seed_history(session, file_path="/media/bad.mkv", priority=0, job_id="not-a-uuid")
    queue = JobQueue(pipeline_runner=_RecordingRunner())

    jobs = rehydrate_pending_jobs(session, queue)

    assert jobs == []
    session.refresh(row)
    assert row.status is JobStatus.FAILED
    assert row.error_text is not None and "malformed job_id" in row.error_text


# ---------------------------------------------------------------------------
# Priority order preserved, both in the returned list and in actual run order.
# ---------------------------------------------------------------------------


def test_rehydrate_preserves_persisted_priority_order(session: Session) -> None:
    _seed_history(session, file_path="/media/third.mkv", priority=5)
    _seed_history(session, file_path="/media/first.mkv", priority=1)
    _seed_history(session, file_path="/media/second.mkv", priority=3)
    queue = JobQueue(pipeline_runner=_RecordingRunner())

    jobs = rehydrate_pending_jobs(session, queue)

    assert [job.file_path.stem for job in jobs] == ["first", "second", "third"]
    assert [job.priority for job in jobs] == [1, 3, 5]


def test_rehydrated_jobs_actually_run_in_persisted_priority_order(session: Session) -> None:
    _seed_history(session, file_path="/media/third.mkv", priority=5)
    _seed_history(session, file_path="/media/first.mkv", priority=1)
    _seed_history(session, file_path="/media/second.mkv", priority=3)
    runner = _RecordingRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)

    rehydrate_pending_jobs(session, queue)
    queue.start()
    assert queue.wait_idle(timeout=5.0)
    queue.shutdown()

    assert runner.order == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# Settings re-derived fresh from current GlobalSettings, not the stored row.
# ---------------------------------------------------------------------------


def test_rehydrate_uses_current_global_settings_not_the_rows_stale_target(
    session: Session,
) -> None:
    """AC: a config change between the row being written and rehydration is reflected."""
    _seed_history(
        session,
        file_path="/media/a.mkv",
        priority=0,
        target="stereo",  # stale -- what was enabled when the row was written
        language="en",
    )
    # Config changes before rehydration runs.
    update_global_settings(
        session,
        enabled_targets=frozenset({DownmixTarget.FIVE_POINT_ONE}),
        language_allow_list=frozenset({"ja"}),
    )
    queue = JobQueue(pipeline_runner=_RecordingRunner())

    (job,) = rehydrate_pending_jobs(session, queue)

    assert job.settings.enabled_targets == frozenset({DownmixTarget.FIVE_POINT_ONE})
    assert job.settings.language_allow_list == frozenset({"ja"})


def test_rehydrate_reconstructs_set_default_audio_job_with_fresh_preference(
    session: Session,
) -> None:
    _seed_history(
        session,
        file_path="/media/a.mkv",
        priority=0,
        kind=JobKind.SET_DEFAULT_AUDIO,
        target="2.1",  # stale
        language="fr",  # stale
    )
    update_global_settings(
        session,
        default_audio_language="en",
        default_audio_channel_tier=DownmixTarget.FIVE_POINT_ONE,
    )
    queue = JobQueue(pipeline_runner=_RecordingRunner())

    (job,) = rehydrate_pending_jobs(session, queue)

    assert job.kind is JobKind.SET_DEFAULT_AUDIO
    assert job.preference == DefaultAudioPreference(
        language="en", channel_tier=DownmixTarget.FIVE_POINT_ONE
    )


def test_rehydrate_skips_set_default_audio_row_when_preference_no_longer_configured(
    session: Session,
) -> None:
    """No Preferred Default Audio configured now -- nothing to re-derive a preference from.

    The row isn't left silently PENDING either (that would be a ghost row
    again) -- it's flipped to FAILED with an explanatory error_text, same as
    any other un-rehydratable row.
    """
    row = _seed_history(
        session,
        file_path="/media/a.mkv",
        priority=0,
        kind=JobKind.SET_DEFAULT_AUDIO,
        target="2.1",
        language="fr",
    )
    queue = JobQueue(pipeline_runner=_RecordingRunner())

    jobs = rehydrate_pending_jobs(session, queue)

    assert jobs == []
    assert queue.list_jobs() == []
    session.refresh(row)
    assert row.status is JobStatus.FAILED
    assert row.error_text is not None and "Default Audio Track" in row.error_text


# ---------------------------------------------------------------------------
# _next_priority seeding (COL-166's correctness fix for COL-163's counter).
# ---------------------------------------------------------------------------


def test_rehydrate_seeds_next_priority_past_every_persisted_row_not_just_pending(
    session: Session,
) -> None:
    """A completed row's priority can exceed any pending row's -- must still be cleared."""
    _seed_history(session, file_path="/media/done.mkv", priority=10, status=JobStatus.SUCCEEDED)
    _seed_history(session, file_path="/media/pending.mkv", priority=2)
    queue = JobQueue(pipeline_runner=_RecordingRunner())

    rehydrate_pending_jobs(session, queue)
    fresh = queue.enqueue("/media/new.mkv", DownmixSettings())

    assert fresh.priority == 11


def test_rehydrate_with_no_history_rows_leaves_next_priority_at_zero(
    session: Session,
) -> None:
    queue = JobQueue(pipeline_runner=_RecordingRunner())

    jobs = rehydrate_pending_jobs(session, queue)
    fresh = queue.enqueue("/media/new.mkv", DownmixSettings())

    assert jobs == []
    assert fresh.priority == 0


def test_freshly_enqueued_job_never_sorts_ahead_of_a_rehydrated_one(session: Session) -> None:
    """The bug COL-166 exists to prevent: a post-restart enqueue jumping the queue."""
    _seed_history(session, file_path="/media/old.mkv", priority=7)
    runner = _RecordingRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)

    rehydrate_pending_jobs(session, queue)
    queue.enqueue("/media/fresh.mkv", DownmixSettings())
    queue.start()
    assert queue.wait_idle(timeout=5.0)
    queue.shutdown()

    assert runner.order == ["old", "fresh"]


# ---------------------------------------------------------------------------
# JobQueue.seed_next_priority / JobQueue.rehydrate primitives, in isolation.
# ---------------------------------------------------------------------------


def test_seed_next_priority_never_lowers_the_counter() -> None:
    queue = JobQueue(pipeline_runner=_RecordingRunner())
    queue.seed_next_priority(10)

    queue.seed_next_priority(3)
    job = queue.enqueue("/media/a.mkv", DownmixSettings())

    assert job.priority == 10


def test_rehydrate_inserts_jobs_without_reassigning_their_priority() -> None:
    queue = JobQueue(pipeline_runner=_RecordingRunner())
    job = Job(
        file_path=Path("/media/a.mkv"),
        settings=DownmixSettings(),
        priority=42,
        status=JobStatus.PENDING,
    )

    queue.rehydrate([job])

    assert queue.get_job(job.id) is job
    assert queue.get_job(job.id).priority == 42  # type: ignore[union-attr]


def test_rehydrate_wakes_an_already_started_pool(session: Session) -> None:
    """rehydrate() called after start() still gets its Job claimed (defensive ordering)."""
    runner = _RecordingRunner()
    queue = JobQueue(max_concurrency=1, pipeline_runner=runner)
    queue.start()

    _seed_history(session, file_path="/media/a.mkv", priority=0)
    rehydrate_pending_jobs(session, queue)
    assert queue.wait_idle(timeout=5.0)
    queue.shutdown()

    assert runner.order == ["a"]
