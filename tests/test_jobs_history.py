"""Tests for job-history persistence and querying (COL-21).

Drives real :class:`~collapsarr.jobs.queue.JobQueue` runs (with an injected
``pipeline_runner`` stub, same convention as ``test_jobs_queue.py``) through
:func:`~collapsarr.jobs.history.record_job_history`, then asserts what ends
up persisted and queryable via the ``session`` fixture (a schema-initialised
DB session -- see ``conftest.py``).

The "automatic persistence" tests below (bottom section) instead wire
:func:`~collapsarr.jobs.history.make_history_recorder` into
:class:`~collapsarr.jobs.queue.JobQueue` itself and prove history shows up
after :meth:`~collapsarr.jobs.queue.JobQueue.run_pending` *without* the test
ever calling ``record_job_history`` -- those build their own engine/session
factory (via the ``settings`` fixture) rather than the single shared
``session`` fixture, since they need a ``sessionmaker`` to hand to
``make_history_recorder``, not a live ``Session``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory, init_db
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.remux import RemuxResult
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.jobs.history import (
    get_job_history,
    list_job_history,
    make_history_recorder,
    record_job_history,
)
from collapsarr.jobs.models import JobHistory
from collapsarr.jobs.queue import Job, JobQueue, JobStatus, PipelineRunner

_SUCCESS = PipelineResult(outcome=PipelineOutcome.SUCCESS, success=True, detail="ok")
_REMUX_FAILURE = PipelineResult(
    outcome=PipelineOutcome.REMUX_FAILED,
    success=False,
    detail="ffmpeg remux failed for '/media/b.mkv' (exit code 1): boom",
    remux_result=RemuxResult(success=False, temp_file_path=None, returncode=1, stderr="boom"),
)


def _stub_runner(result: PipelineResult) -> PipelineRunner:
    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        return result

    return runner


# ---------------------------------------------------------------------------
# Persisting a job run.
# ---------------------------------------------------------------------------


def test_record_job_history_persists_a_queued_job(session: Session) -> None:
    """A never-run (PENDING) job persists as queued, with no timestamps yet."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    history = record_job_history(session, job)

    assert history.id is not None
    assert history.job_id == str(job.id)
    assert history.file_path == "/media/movie.mkv"
    assert history.status is JobStatus.PENDING
    assert history.started_at is None
    assert history.ended_at is None
    assert history.exit_code is None
    assert history.error_text is None


def test_record_job_history_persists_a_succeeded_run(session: Session) -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    settings = DownmixSettings(
        enabled_targets=frozenset({DownmixTarget.STEREO, DownmixTarget.FIVE_POINT_ONE}),
        language_allow_list=frozenset({"eng", "jpn"}),
    )
    job = queue.enqueue("/media/movie.mkv", settings)
    queue.run_pending()

    history = record_job_history(session, job)

    assert history.status is JobStatus.SUCCEEDED
    assert history.started_at is not None
    assert history.ended_at is not None
    assert history.started_at <= history.ended_at
    assert history.exit_code is None  # no remux stage on this stub result
    assert history.error_text is None
    assert history.target == "5.1,stereo"
    assert history.language == "eng,jpn"


def test_record_job_history_persists_exit_code_and_error_on_remux_failure(
    session: Session,
) -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_REMUX_FAILURE))
    job = queue.enqueue("/media/b.mkv", DownmixSettings())
    queue.run_pending()

    history = record_job_history(session, job)

    assert history.status is JobStatus.FAILED
    assert history.exit_code == 1
    assert history.error_text == _REMUX_FAILURE.detail


def test_record_job_history_persists_error_text_for_an_unexpected_exception(
    session: Session,
) -> None:
    def raising_runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        raise RuntimeError("boom")

    queue = JobQueue(pipeline_runner=raising_runner)
    job = queue.enqueue("/media/c.mkv", DownmixSettings())
    queue.run_pending()

    history = record_job_history(session, job)

    assert history.status is JobStatus.FAILED
    assert history.exit_code is None
    assert history.error_text == "boom"


def test_record_job_history_defaults_language_to_none_when_unrestricted(
    session: Session,
) -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings(language_allow_list=None))

    history = record_job_history(session, job)

    assert history.language is None
    assert history.target == "stereo"  # DownmixSettings default


def test_record_job_history_upserts_the_same_row_across_lifecycle_calls(
    session: Session,
) -> None:
    """Calling record_job_history again for the same job updates, not duplicates."""
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queued_history = record_job_history(session, job)
    queue.run_pending()
    finished_history = record_job_history(session, job)

    assert finished_history.id == queued_history.id
    assert finished_history.status is JobStatus.SUCCEEDED
    assert list_job_history(session) == [finished_history]


# ---------------------------------------------------------------------------
# Querying: list (with filters) and get-by-id.
# ---------------------------------------------------------------------------


def _record(session: Session, queue: JobQueue, file_path: str, result: PipelineResult) -> Job:
    """Enqueue, run, and persist one job for the given (single-result) queue."""
    job = queue.enqueue(file_path, DownmixSettings())
    queue.run_pending()
    record_job_history(session, job)
    return job


def test_list_job_history_with_no_filters_returns_everything_in_order(session: Session) -> None:
    queue_a = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    queue_b = JobQueue(pipeline_runner=_stub_runner(_REMUX_FAILURE))
    _record(session, queue_a, "/media/a.mkv", _SUCCESS)
    _record(session, queue_b, "/media/b.mkv", _REMUX_FAILURE)

    rows = list_job_history(session)

    assert [row.file_path for row in rows] == ["/media/a.mkv", "/media/b.mkv"]


def test_list_job_history_filters_by_file_path(session: Session) -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    _record(session, queue, "/media/a.mkv", _SUCCESS)
    _record(session, queue, "/media/b.mkv", _SUCCESS)

    rows = list_job_history(session, file_path="/media/a.mkv")

    assert [row.file_path for row in rows] == ["/media/a.mkv"]


def test_list_job_history_filters_by_file_path_accepting_a_path_object(
    session: Session,
) -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    _record(session, queue, "/media/a.mkv", _SUCCESS)

    rows = list_job_history(session, file_path=Path("/media/a.mkv"))

    assert len(rows) == 1


def test_list_job_history_filters_by_status(session: Session) -> None:
    queue_ok = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    queue_fail = JobQueue(pipeline_runner=_stub_runner(_REMUX_FAILURE))
    _record(session, queue_ok, "/media/a.mkv", _SUCCESS)
    _record(session, queue_fail, "/media/b.mkv", _REMUX_FAILURE)

    failed_rows = list_job_history(session, status=JobStatus.FAILED)
    succeeded_rows = list_job_history(session, status=JobStatus.SUCCEEDED)

    assert [row.file_path for row in failed_rows] == ["/media/b.mkv"]
    assert [row.file_path for row in succeeded_rows] == ["/media/a.mkv"]


def test_list_job_history_combines_file_and_status_filters(session: Session) -> None:
    queue_ok = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    queue_fail = JobQueue(pipeline_runner=_stub_runner(_REMUX_FAILURE))
    _record(session, queue_ok, "/media/a.mkv", _SUCCESS)
    _record(session, queue_fail, "/media/a.mkv", _REMUX_FAILURE)

    rows = list_job_history(session, file_path="/media/a.mkv", status=JobStatus.FAILED)

    assert len(rows) == 1
    assert rows[0].status is JobStatus.FAILED


def test_list_job_history_returns_empty_list_when_nothing_matches(session: Session) -> None:
    assert list_job_history(session) == []
    assert list_job_history(session, file_path="/nope.mkv") == []
    assert list_job_history(session, status=JobStatus.RUNNING) == []


def test_get_job_history_returns_the_matching_row(session: Session) -> None:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    history = record_job_history(session, job)

    by_uuid = get_job_history(session, job.id)
    assert by_uuid is not None
    assert by_uuid.id == history.id
    assert get_job_history(session, str(job.id)) is not None


def test_get_job_history_returns_none_for_an_unknown_job_id(session: Session) -> None:
    assert get_job_history(session, uuid4()) is None


def test_job_history_importable_from_package_root() -> None:
    """Sanity check the public re-exports from collapsarr.jobs."""
    from collapsarr.jobs import JobHistory as ReexportedJobHistory
    from collapsarr.jobs import get_job_history as reexported_get
    from collapsarr.jobs import list_job_history as reexported_list
    from collapsarr.jobs import make_history_recorder as reexported_make_recorder
    from collapsarr.jobs import record_job_history as reexported_record

    assert ReexportedJobHistory is JobHistory
    assert reexported_get is get_job_history
    assert reexported_list is list_job_history
    assert reexported_record is record_job_history
    assert reexported_make_recorder is make_history_recorder


# ---------------------------------------------------------------------------
# Automatic persistence: JobQueue(history_recorder=...) requires no explicit
# record_job_history call from the caller.
# ---------------------------------------------------------------------------


def test_run_pending_automatically_persists_history_when_a_recorder_is_configured(
    settings: Settings,
) -> None:
    """Every job run is persisted by run_pending() itself -- no manual call."""
    engine = create_engine_from_settings(settings)
    init_db(engine)
    session_factory = create_session_factory(engine)

    queue = JobQueue(
        pipeline_runner=_stub_runner(_SUCCESS),
        history_recorder=make_history_recorder(session_factory),
    )
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.run_pending()  # note: no record_job_history(...) call anywhere here

    with session_factory() as read_session:
        rows = list_job_history(read_session)

    assert len(rows) == 1
    assert rows[0].job_id == str(job.id)
    assert rows[0].file_path == "/media/movie.mkv"
    assert rows[0].status is JobStatus.SUCCEEDED
    assert rows[0].started_at is not None
    assert rows[0].ended_at is not None
    engine.dispose()


def test_run_pending_automatically_persists_a_failed_job(settings: Settings) -> None:
    engine = create_engine_from_settings(settings)
    init_db(engine)
    session_factory = create_session_factory(engine)

    queue = JobQueue(
        pipeline_runner=_stub_runner(_REMUX_FAILURE),
        history_recorder=make_history_recorder(session_factory),
    )
    queue.enqueue("/media/b.mkv", DownmixSettings())

    queue.run_pending()

    with session_factory() as read_session:
        rows = list_job_history(read_session, status=JobStatus.FAILED)

    assert len(rows) == 1
    assert rows[0].exit_code == 1
    assert rows[0].error_text == _REMUX_FAILURE.detail


def test_run_pending_with_no_history_recorder_persists_nothing(settings: Settings) -> None:
    """No history_recorder configured -> run_pending works, nothing persisted."""
    engine = create_engine_from_settings(settings)
    init_db(engine)
    session_factory = create_session_factory(engine)

    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))  # no history_recorder
    queue.enqueue("/media/movie.mkv", DownmixSettings())

    jobs = queue.run_pending()

    assert jobs[0].status is JobStatus.SUCCEEDED  # the job itself still ran fine
    with session_factory() as read_session:
        assert list_job_history(read_session) == []
    engine.dispose()


def test_run_pending_persists_history_for_every_concurrently_run_job(
    settings: Settings,
) -> None:
    """A stronger proof that make_history_recorder is safe under real concurrency:

    max_concurrency=3 means up to 3 worker threads call the recorder at
    once; every one of 6 jobs must still land its own row, with no lost
    writes or cross-thread session sharing errors.
    """
    engine = create_engine_from_settings(settings)
    init_db(engine)
    session_factory = create_session_factory(engine)

    queue = JobQueue(
        max_concurrency=3,
        pipeline_runner=_stub_runner(_SUCCESS),
        history_recorder=make_history_recorder(session_factory),
    )
    jobs = [queue.enqueue(f"/media/{i}.mkv", DownmixSettings()) for i in range(6)]

    queue.run_pending()

    with session_factory() as read_session:
        rows = list_job_history(read_session)

    assert {row.job_id for row in rows} == {str(job.id) for job in jobs}
    assert all(row.status is JobStatus.SUCCEEDED for row in rows)
    engine.dispose()


def test_from_settings_threads_history_recorder_through(settings: Settings) -> None:
    engine = create_engine_from_settings(settings)
    init_db(engine)
    session_factory = create_session_factory(engine)

    queue = JobQueue.from_settings(
        settings,
        pipeline_runner=_stub_runner(_SUCCESS),
        history_recorder=make_history_recorder(session_factory),
    )
    queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.run_pending()

    with session_factory() as read_session:
        assert len(list_job_history(read_session)) == 1
    engine.dispose()


def test_from_settings_with_no_history_recorder_arg_still_persists_by_default(
    settings: Settings,
) -> None:
    """The production path: JobQueue.from_settings() with zero extra plumbing.

    Unlike the raw JobQueue() constructor (where history_recorder stays
    None unless given), from_settings() is the factory real callers (and
    COL-22's future scheduler wiring) use, so it must default to a real
    recorder -- proven here by never passing history_recorder, or touching
    make_history_recorder/record_job_history, anywhere in this test.
    """
    queue = JobQueue.from_settings(settings, pipeline_runner=_stub_runner(_SUCCESS))
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.run_pending()

    # Read back through an independent engine/session -- proves the data
    # actually landed in settings' database, not just in some in-memory
    # stub, and that from_settings() built its own working session factory
    # internally rather than silently doing nothing.
    read_engine = create_engine_from_settings(settings)
    with create_session_factory(read_engine)() as read_session:
        rows = list_job_history(read_session)
    read_engine.dispose()

    assert len(rows) == 1
    assert rows[0].job_id == str(job.id)
    assert rows[0].file_path == "/media/movie.mkv"
    assert rows[0].status is JobStatus.SUCCEEDED
