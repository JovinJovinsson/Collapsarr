"""Tests for wiring the webhook + periodic scan into the job queue (COL-22).

:class:`~collapsarr.jobs.scheduler.JobScheduler` is driven with injectable
seams -- a stub ``probe`` (so no ``ffprobe`` or real media is needed), a
controllable ``now`` clock (for the dedup window), an injected
``pipeline_runner`` on the queue (same convention as ``test_jobs_queue.py``),
and a monkeypatched ``fetch_monitored_files`` (so the scan needs no live
Sonarr/Radarr). Job-history dedup is exercised against a real schema-initialised
SQLite database built from the ``settings`` fixture.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.arr.files import MonitoredFile
from collapsarr.arr.models import ArrInstance, InstanceType, RemotePathMapping
from collapsarr.arr.webhooks import ResolvedWebhookFile
from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory, init_db
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.probe import AudioStreamInfo, FfprobeError
from collapsarr.downmix.targets import DownmixSettings
from collapsarr.jobs import scheduler as scheduler_module
from collapsarr.jobs.history import record_job_history
from collapsarr.jobs.queue import Job, JobQueue, JobStatus, PipelineRunner
from collapsarr.jobs.scheduler import JobScheduler

# A 5.1 stream: with default (Stereo) settings, Stereo (2ch < 6ch, not present)
# qualifies -> enqueue.
_SURROUND: list[AudioStreamInfo] = [
    AudioStreamInfo(index=0, codec="ac3", channels=6, channel_layout="5.1(side)", language="eng")
]
# A stereo-only stream: Stereo already present at 2ch -> nothing qualifies.
_STEREO_ONLY: list[AudioStreamInfo] = [
    AudioStreamInfo(index=0, codec="aac", channels=2, channel_layout="stereo", language="eng")
]

_SUCCESS = PipelineResult(outcome=PipelineOutcome.SUCCESS, success=True, detail="ok")
_FIXED_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)


def _stub_runner(result: PipelineResult = _SUCCESS) -> PipelineRunner:
    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        return result

    return runner


def _probe_returning(streams: Sequence[AudioStreamInfo]) -> scheduler_module.ProbeFn:
    def probe(path: Path) -> Sequence[AudioStreamInfo]:
        return streams

    return probe


def _probe_raising() -> scheduler_module.ProbeFn:
    def probe(path: Path) -> Sequence[AudioStreamInfo]:
        raise FfprobeError("ffprobe not found")

    return probe


@pytest.fixture
def session_factory(settings: Settings) -> Iterator[sessionmaker[Session]]:
    """A schema-initialised session factory over the isolated ``settings`` DB."""
    engine = create_engine_from_settings(settings)
    init_db(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _make_scheduler(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    probe: scheduler_module.ProbeFn,
    queue: JobQueue | None = None,
    now: datetime = _FIXED_NOW,
) -> JobScheduler:
    return JobScheduler(
        queue or JobQueue(pipeline_runner=_stub_runner()),
        session_factory,
        settings,
        probe=probe,
        now=lambda: now,
    )


def _add_instance(
    session_factory: sessionmaker[Session],
    *,
    instance_type: InstanceType = InstanceType.SONARR,
    name: str = "inst",
    mappings: list[tuple[str, str]] | None = None,
) -> ArrInstance:
    with session_factory() as session:
        instance = ArrInstance(
            name=name, type=instance_type, base_url="http://arr.local", api_key="k"
        )
        session.add(instance)
        session.commit()
        session.refresh(instance)
        for order, (remote, local) in enumerate(mappings or []):
            session.add(
                RemotePathMapping(
                    instance_id=instance.id, remote_prefix=remote, local_prefix=local, order=order
                )
            )
        session.commit()
        session.refresh(instance)
        return instance


# ---------------------------------------------------------------------------
# enqueue_file: qualifying-target gate.
# ---------------------------------------------------------------------------


def test_enqueue_file_enqueues_a_job_when_a_target_qualifies(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.enqueue_file("/media/movie.mkv")

    assert job is not None
    assert job.file_path == Path("/media/movie.mkv")
    assert job.status is JobStatus.PENDING
    assert [j.id for j in scheduler._queue.list_jobs()] == [job.id]


def test_enqueue_file_skips_a_file_with_nothing_to_do(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_STEREO_ONLY))

    assert scheduler.enqueue_file("/media/movie.mkv") is None
    assert scheduler._queue.list_jobs() == []


def test_enqueue_file_skips_a_file_that_cannot_be_probed(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_raising())

    assert scheduler.enqueue_file("/media/movie.mkv") is None
    assert scheduler._queue.list_jobs() == []


# ---------------------------------------------------------------------------
# De-duplication.
# ---------------------------------------------------------------------------


def test_enqueue_file_skips_an_already_pending_file(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    first = scheduler.enqueue_file("/media/movie.mkv")
    second = scheduler.enqueue_file("/media/movie.mkv")

    assert first is not None
    assert second is None
    assert len(scheduler._queue.list_jobs()) == 1


def test_enqueue_file_skips_a_currently_running_file(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.enqueue_file("/media/movie.mkv")
    assert job is not None
    job.status = JobStatus.RUNNING  # simulate the worker having picked it up

    assert scheduler.enqueue_file("/media/movie.mkv") is None
    assert len(scheduler._queue.list_jobs()) == 1


def test_enqueue_file_re_enqueues_after_a_terminal_job(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A SUCCEEDED/FAILED job is not 'active' -- only the dedup window guards it."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.enqueue_file("/media/movie.mkv")
    assert job is not None
    job.status = JobStatus.SUCCEEDED  # in-memory only; no history row persisted

    # No history row -> not "recently processed" -> re-enqueue is allowed.
    assert scheduler.enqueue_file("/media/movie.mkv") is not None
    assert len(scheduler._queue.list_jobs()) == 2


def test_enqueue_file_skips_a_recently_processed_file(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session:
        _record_terminal(session, "/media/movie.mkv", ended_at=_FIXED_NOW)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        assert scheduler.enqueue_file("/media/movie.mkv", session=session) is None
    assert scheduler._queue.list_jobs() == []


def test_enqueue_file_re_enqueues_a_file_processed_before_the_window(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    # Default window is scan_interval_hours (6h); 7h ago is outside it.
    ended = _FIXED_NOW - timedelta(hours=7)
    with session_factory() as session:
        _record_terminal(session, "/media/movie.mkv", ended_at=ended)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        job = scheduler.enqueue_file("/media/movie.mkv", session=session)
    assert job is not None


def _record_terminal(
    session: Session,
    file_path: str,
    *,
    ended_at: datetime,
    status: JobStatus = JobStatus.SUCCEEDED,
) -> None:
    job = Job(
        file_path=Path(file_path),
        settings=DownmixSettings(),
        status=status,
        started_at=ended_at,
        ended_at=ended_at,
    )
    record_job_history(session, job)


# ---------------------------------------------------------------------------
# Webhook "file ready" hook.
# ---------------------------------------------------------------------------


def _resolved(file_path: str) -> ResolvedWebhookFile:
    return ResolvedWebhookFile(
        instance_id=1,
        instance_name="inst",
        media_title="Movie",
        file_path=file_path,
        is_upgrade=False,
    )


def test_on_file_ready_enqueues_a_real_job(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    scheduler.on_file_ready(_resolved("/media/movie.mkv"))

    jobs = scheduler._queue.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].file_path == Path("/media/movie.mkv")


def test_on_file_ready_does_not_enqueue_when_nothing_to_do(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_STEREO_ONLY))

    scheduler.on_file_ready(_resolved("/media/movie.mkv"))

    assert scheduler._queue.list_jobs() == []


# ---------------------------------------------------------------------------
# Full-library scan.
# ---------------------------------------------------------------------------


def _patch_fetch(
    monkeypatch: pytest.MonkeyPatch,
    files_by_instance: dict[int, list[MonitoredFile]] | None = None,
    *,
    failing_instance_ids: set[int] | None = None,
) -> None:
    failing = failing_instance_ids or set()
    mapping = files_by_instance or {}

    def fake_fetch(instance: ArrInstance, **_: object) -> list[MonitoredFile]:
        if instance.id in failing:
            raise httpx.ConnectError("connection refused")
        return mapping.get(instance.id, [])

    monkeypatch.setattr(scheduler_module, "fetch_monitored_files", fake_fetch)


def test_scan_once_enqueues_qualifying_files_and_resolves_paths(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _add_instance(session_factory, mappings=[("/tv", "/mnt/media/tv")])
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(instance_id=instance.id, media_title="Show", file_path="/tv/a.mkv")
            ]
        },
    )
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    enqueued = scheduler.scan_once()

    assert len(enqueued) == 1
    assert enqueued[0].file_path == Path("/mnt/media/tv/a.mkv")


def test_scan_once_skips_files_with_nothing_to_do(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(instance_id=instance.id, media_title="Show", file_path="/tv/a.mkv")
            ]
        },
    )
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_STEREO_ONLY))

    assert scheduler.scan_once() == []


def test_scan_once_dedupes_a_file_already_enqueued_by_a_prior_pass(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(instance_id=instance.id, media_title="Show", file_path="/tv/a.mkv")
            ]
        },
    )
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    first = scheduler.scan_once()
    second = scheduler.scan_once()  # overlapping trigger: same file still PENDING

    assert len(first) == 1
    assert second == []
    assert len(scheduler._queue.list_jobs()) == 1


def test_scan_once_continues_past_an_unreachable_instance(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _add_instance(session_factory, name="down")
    good = _add_instance(session_factory, name="up")
    _patch_fetch(
        monkeypatch,
        {
            good.id: [
                MonitoredFile(instance_id=good.id, media_title="Show", file_path="/tv/a.mkv")
            ]
        },
        failing_instance_ids={bad.id},
    )
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    enqueued = scheduler.scan_once()

    assert len(enqueued) == 1
    assert enqueued[0].file_path == Path("/tv/a.mkv")


# ---------------------------------------------------------------------------
# Background loop: triggering + lifecycle.
# ---------------------------------------------------------------------------


def test_start_runs_an_initial_scan_and_drains_the_queue(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting the loop triggers a scan and runs what it enqueues."""
    _add_instance(session_factory)
    scanned = threading.Event()

    def fake_fetch(inst: ArrInstance, **_: object) -> list[MonitoredFile]:
        scanned.set()
        return [MonitoredFile(instance_id=inst.id, media_title="Show", file_path="/tv/a.mkv")]

    monkeypatch.setattr(scheduler_module, "fetch_monitored_files", fake_fetch)
    queue = JobQueue(pipeline_runner=_stub_runner())
    scheduler = _make_scheduler(
        settings, session_factory, probe=_probe_returning(_SURROUND), queue=queue
    )

    scheduler.start()
    try:
        assert scanned.wait(timeout=5.0), "background scan did not run"
        _wait_until(lambda: _all_terminal(queue), timeout=5.0)
    finally:
        scheduler.stop()

    jobs = queue.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].status is JobStatus.SUCCEEDED


def test_start_twice_raises(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_fetch(monkeypatch)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    scheduler.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            scheduler.start()
    finally:
        scheduler.stop()


def test_stop_is_safe_before_start(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    scheduler.stop()  # must not raise


def _all_terminal(queue: JobQueue) -> bool:
    jobs = queue.list_jobs()
    terminal = (JobStatus.SUCCEEDED, JobStatus.FAILED)
    return bool(jobs) and all(job.status in terminal for job in jobs)


def _wait_until(predicate: object, *, timeout: float) -> None:
    import time

    deadline = time.monotonic() + timeout
    assert callable(predicate)
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")
