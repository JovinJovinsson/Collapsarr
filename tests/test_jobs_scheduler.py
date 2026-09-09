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

import logging
import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.arr.catalog import CatalogEpisode, CatalogSeries, SonarrCatalog
from collapsarr.arr.files import MonitoredFile
from collapsarr.arr.models import ArrInstance, InstanceType, RemotePathMapping
from collapsarr.arr.webhooks import ResolvedWebhookFile
from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.downmix.cancellation import CancellationHandle
from collapsarr.downmix.default_audio import DefaultAudioPreference
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.probe import AudioStreamInfo, FfprobeError
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.jobs import scheduler as scheduler_module
from collapsarr.jobs.history import get_job_history, record_job_history
from collapsarr.jobs.queue import (
    DefaultAudioPipelineRunner,
    Job,
    JobKind,
    JobQueue,
    JobStatus,
    PipelineRunner,
)
from collapsarr.jobs.scheduler import AUTO_QUEUE_LIMIT, DefaultAudioSkipReason, JobScheduler
from collapsarr.library.service import set_tracked, upsert_series_episode_node
from collapsarr.media.service import get_tracked_media
from collapsarr.migrations import upgrade_to_head
from collapsarr.settings.models import DEFAULT_RECENTLY_PROCESSED_WINDOW_MINUTES
from collapsarr.settings.service import update_global_settings

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
_FAILURE = PipelineResult(outcome=PipelineOutcome.REMUX_FAILED, success=False, detail="boom")
_FIXED_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)


def _stub_runner(result: PipelineResult = _SUCCESS) -> PipelineRunner:
    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        return result

    return runner


def _stub_default_audio_runner(result: PipelineResult = _SUCCESS) -> DefaultAudioPipelineRunner:
    def runner(
        file_path: Path, preference: DefaultAudioPreference, **_: object
    ) -> PipelineResult:
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
    upgrade_to_head(settings)
    yield create_session_factory(engine)
    engine.dispose()


def _make_scheduler(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    probe: scheduler_module.ProbeFn,
    queue: JobQueue | None = None,
    now: datetime = _FIXED_NOW,
    downmix_settings: DownmixSettings | None = None,
) -> JobScheduler:
    return JobScheduler(
        queue or JobQueue(pipeline_runner=_stub_runner()),
        session_factory,
        settings,
        probe=probe,
        now=lambda: now,
        downmix_settings=downmix_settings,
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
    # Default window is recently_processed_window_minutes (COL-167; 360min =
    # 6h); 7h ago is outside it.
    ended = _FIXED_NOW - timedelta(hours=7)
    with session_factory() as session:
        _record_terminal(session, "/media/movie.mkv", ended_at=ended)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        job = scheduler.enqueue_file("/media/movie.mkv", session=session)
    assert job is not None


def test_enqueue_file_respects_a_configured_recently_processed_window(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """COL-167: the dedup window is a settings-driven value, not scan_interval_hours."""
    with session_factory() as session:
        update_global_settings(session, recently_processed_window_minutes=30)
        # 45 minutes ago is outside a 30-minute window, even though it's well
        # inside the default 360-minute (6h) window -- proving the check uses
        # the configured value, not the default.
        _record_terminal(session, "/media/movie.mkv", ended_at=_FIXED_NOW - timedelta(minutes=45))

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        job = scheduler.enqueue_file("/media/movie.mkv", session=session)
    assert job is not None


def test_enqueue_file_recently_processed_window_reloads_live_without_restart(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """COL-167: a PATCH-style settings change is visible on the *next* check --

    no scheduler reconstruction, and the value is not cached at ``__init__``.
    """
    with session_factory() as session:
        _record_terminal(session, "/media/movie.mkv", ended_at=_FIXED_NOW - timedelta(minutes=45))

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    # Default window (360min) still covers a 45-minute-old terminal row.
    with session_factory() as session:
        assert scheduler.enqueue_file("/media/movie.mkv", session=session) is None

    # Simulate the PATCH settings endpoint narrowing the window -- same
    # scheduler instance, no reconstruction.
    with session_factory() as session:
        update_global_settings(session, recently_processed_window_minutes=30)

    with session_factory() as session:
        job = scheduler.enqueue_file("/media/movie.mkv", session=session)
    assert job is not None


def test_enqueue_file_recently_processed_window_zero_disables_the_cooldown(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """COL-167: ``0`` means "no cooldown, always allow retry" -- even seconds-old."""
    with session_factory() as session:
        update_global_settings(session, recently_processed_window_minutes=0)
        _record_terminal(session, "/media/movie.mkv", ended_at=_FIXED_NOW - timedelta(seconds=1))

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


def test_on_file_ready_persists_the_library_node_bridge_ids(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """The webhook's episode/movie id lands on the tracked-media row (COL-101)."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    file = ResolvedWebhookFile(
        instance_id=7,
        instance_name="inst",
        media_title="Show",
        file_path="/media/movie.mkv",
        is_upgrade=False,
        sonarr_episode_id=101,
    )

    scheduler.on_file_ready(file)

    with session_factory() as session:
        media = get_tracked_media(session, "/media/movie.mkv")
        assert media is not None
        assert media.instance_id == 7
        assert media.sonarr_episode_id == 101
        assert media.radarr_movie_id is None


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


def test_last_scan_at_is_none_before_the_first_scan(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """COL-122: ``last_scan_at`` starts unset so ``GET /api/system/tasks`` can
    report the Library Scan Scheduled Task as never having run yet."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    assert scheduler.last_scan_at is None


def test_scan_once_stamps_last_scan_at_with_the_scheduler_clock(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """COL-122: ``last_scan_at`` is stamped at the start of every
    :meth:`~collapsarr.jobs.scheduler.JobScheduler.scan_once` run, from the
    injectable clock (not real wall-clock time) -- no configured instances are
    needed since the stamp happens before the per-instance loop runs."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    scheduler.scan_once()

    assert scheduler.last_scan_at == _FIXED_NOW


def test_scan_once_persists_the_library_node_bridge_ids(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scanned file's Sonarr episode id lands on its tracked-media row (COL-101)."""
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(
                    instance_id=instance.id,
                    media_title="Show",
                    file_path="/tv/a.mkv",
                    sonarr_episode_id=101,
                )
            ]
        },
    )
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    scheduler.scan_once()

    with session_factory() as session:
        media = get_tracked_media(session, "/tv/a.mkv")
        assert media is not None
        assert media.instance_id == instance.id
        assert media.sonarr_episode_id == 101


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
# Manual on-demand triggers (COL-23).
# ---------------------------------------------------------------------------


# A jpn-only 5.1 stream, used to exercise the language allow-list bypass:
# under an eng-only allow-list this language is invisible unless the trigger
# explicitly widens it via `extra_languages`.
_SURROUND_JPN: list[AudioStreamInfo] = [
    AudioStreamInfo(index=0, codec="ac3", channels=6, channel_layout="5.1(side)", language="jpn")
]


def test_scan_now_runs_the_scan_immediately(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`scan_now` is the manual-trigger entry point -- equivalent to `scan_once`."""
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

    enqueued = scheduler.scan_now()

    assert len(enqueued) == 1
    assert enqueued[0].file_path == Path("/tv/a.mkv")


def test_scan_now_works_standalone_without_the_background_loop(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual scan-now must not require `start()` to ever have been called."""
    _patch_fetch(monkeypatch, {})
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    assert scheduler.scan_now() == []  # no instances configured; must not raise


def test_trigger_file_enqueues_like_enqueue_file_by_default(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.trigger_file("/media/movie.mkv")

    assert job is not None
    assert job.file_path == Path("/media/movie.mkv")


def test_trigger_file_respects_dedup(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    first = scheduler.trigger_file("/media/movie.mkv")
    second = scheduler.trigger_file("/media/movie.mkv")

    assert first is not None
    assert second is None
    assert len(scheduler._queue.list_jobs()) == 1


def test_trigger_file_with_extra_languages_bypasses_the_allow_list(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A jpn-only stream is invisible under an eng-only allow-list -- unless bypassed."""
    downmix_settings = DownmixSettings(language_allow_list=frozenset({"eng"}))
    scheduler = _make_scheduler(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND_JPN),
        downmix_settings=downmix_settings,
    )

    # Without the bypass, the global allow-list excludes "jpn" entirely.
    assert scheduler.trigger_file("/media/movie.mkv") is None
    assert scheduler._queue.list_jobs() == []

    # With the bypass, "jpn" is unioned in for this one call and qualifies.
    job = scheduler.trigger_file("/media/movie.mkv", extra_languages={"jpn"})

    assert job is not None
    assert job.settings.language_allow_list == frozenset({"eng", "jpn"})


def test_trigger_file_extra_languages_does_not_mutate_scheduler_settings(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """The override is scoped to one call -- the automatic triggers stay unaffected."""
    downmix_settings = DownmixSettings(language_allow_list=frozenset({"eng"}))
    scheduler = _make_scheduler(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND_JPN),
        downmix_settings=downmix_settings,
    )

    job = scheduler.trigger_file("/media/movie.mkv", extra_languages={"jpn"})
    assert job is not None

    # The scheduler's own settings (used by webhook/scan triggers) is untouched.
    assert scheduler._downmix_settings.language_allow_list == frozenset({"eng"})
    # A fresh automatic-path enqueue of a jpn-only file is still excluded.
    assert scheduler.enqueue_file("/media/other.mkv") is None


def test_trigger_file_extra_languages_is_a_noop_when_allow_list_is_none(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """No allow-list already evaluates every language -- extra_languages changes nothing."""
    scheduler = _make_scheduler(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND_JPN),
        downmix_settings=DownmixSettings(),  # language_allow_list=None
    )

    job = scheduler.trigger_file("/media/movie.mkv", extra_languages={"jpn"})

    assert job is not None
    assert job.settings.language_allow_list is None


def test_trigger_file_skips_a_file_with_nothing_to_do_even_with_extra_languages(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """extra_languages only widens languages considered, not the qualifying-target gate."""
    downmix_settings = DownmixSettings(language_allow_list=frozenset({"eng"}))
    stereo_jpn: list[AudioStreamInfo] = [
        AudioStreamInfo(index=0, codec="aac", channels=2, channel_layout="stereo", language="jpn")
    ]
    scheduler = _make_scheduler(
        settings,
        session_factory,
        probe=_probe_returning(stereo_jpn),
        downmix_settings=downmix_settings,
    )

    assert scheduler.trigger_file("/media/movie.mkv", extra_languages={"jpn"}) is None


# ---------------------------------------------------------------------------
# bypass_dedup_window (COL-170)
# ---------------------------------------------------------------------------


def test_enqueue_file_bypass_dedup_window_ignores_a_recently_processed_history_row(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """``bypass_dedup_window=True`` skips the recently-processed half of the check."""
    with session_factory() as session:
        _record_terminal(session, "/media/movie.mkv", ended_at=_FIXED_NOW)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        # Without the bypass this would be skipped (see
        # test_enqueue_file_skips_a_recently_processed_file).
        job = scheduler.enqueue_file(
            "/media/movie.mkv", session=session, bypass_dedup_window=True
        )
    assert job is not None


def test_enqueue_file_bypass_dedup_window_still_treats_an_active_job_as_a_duplicate(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """The bypass only skips the *recently processed* half -- not "already active"."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.enqueue_file("/media/movie.mkv")
    assert job is not None  # still PENDING -> active

    second = scheduler.enqueue_file("/media/movie.mkv", bypass_dedup_window=True)

    assert second is None
    assert len(scheduler._queue.list_jobs()) == 1


def test_trigger_file_bypass_dedup_window_ignores_a_recently_processed_history_row(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """``trigger_file``'s ``bypass_dedup_window`` threads straight through to ``enqueue_file``."""
    with session_factory() as session:
        _record_terminal(session, "/media/movie.mkv", ended_at=_FIXED_NOW)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        job = scheduler.trigger_file(
            "/media/movie.mkv", session=session, bypass_dedup_window=True
        )
    assert job is not None


def test_trigger_file_defaults_to_respecting_a_recently_processed_history_row(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """Without the flag, ``trigger_file`` still respects the window (unchanged default)."""
    with session_factory() as session:
        _record_terminal(session, "/media/movie.mkv", ended_at=_FIXED_NOW)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        assert scheduler.trigger_file("/media/movie.mkv", session=session) is None


# ---------------------------------------------------------------------------
# requeue_file (COL-170)
# ---------------------------------------------------------------------------


def test_requeue_file_enqueues_despite_a_recently_processed_history_row(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A per-row Requeue always bypasses the Recently-Processed Window."""
    with session_factory() as session:
        _record_terminal(session, "/media/movie.mkv", ended_at=_FIXED_NOW, status=JobStatus.FAILED)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        job = scheduler.requeue_file("/media/movie.mkv", session=session)

    assert job is not None
    assert job.file_path == Path("/media/movie.mkv")


def test_requeue_file_still_treats_an_active_job_as_a_duplicate(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """Requeue bypasses the window, not the "already active" duplicate check."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    first = scheduler.requeue_file("/media/movie.mkv")
    assert first is not None  # still PENDING -> active

    second = scheduler.requeue_file("/media/movie.mkv")

    assert second is None
    assert len(scheduler._queue.list_jobs()) == 1


def test_requeue_file_skips_a_file_with_nothing_to_do(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """Bypassing the window never means "enqueue even a file with no qualifying target"."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_STEREO_ONLY))

    assert scheduler.requeue_file("/media/movie.mkv") is None
    assert scheduler._queue.list_jobs() == []


# ---------------------------------------------------------------------------
# requeue_all_failed (COL-172)
# ---------------------------------------------------------------------------


def test_requeue_all_failed_requeues_every_currently_failed_file(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: full success -- every currently-FAILED file outside the window is requeued."""
    ended = _FIXED_NOW - timedelta(hours=7)  # outside the default 360min window
    with session_factory() as session:
        _record_terminal(session, "/media/a.mkv", ended_at=ended, status=JobStatus.FAILED)
        _record_terminal(session, "/media/b.mkv", ended_at=ended, status=JobStatus.FAILED)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        outcome = scheduler.requeue_all_failed(session=session)

    assert {job.file_path for job in outcome.requeued} == {
        Path("/media/a.mkv"),
        Path("/media/b.mkv"),
    }
    assert outcome.skipped == []


def test_requeue_all_failed_skips_files_inside_the_recently_processed_window(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: partial skip -- unlike requeue_file, the bulk action respects the window."""
    with session_factory() as session:
        # Inside the default 360min window -> skipped.
        _record_terminal(session, "/media/a.mkv", ended_at=_FIXED_NOW, status=JobStatus.FAILED)
        # Outside it -> requeued.
        _record_terminal(
            session,
            "/media/b.mkv",
            ended_at=_FIXED_NOW - timedelta(hours=7),
            status=JobStatus.FAILED,
        )

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        outcome = scheduler.requeue_all_failed(session=session)

    assert [job.file_path for job in outcome.requeued] == [Path("/media/b.mkv")]
    assert outcome.skipped == ["/media/a.mkv"]


def test_requeue_all_failed_all_skipped_is_a_valid_empty_requeued_result(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: every currently-FAILED file inside the window -> all-skipped is not an error."""
    with session_factory() as session:
        _record_terminal(session, "/media/a.mkv", ended_at=_FIXED_NOW, status=JobStatus.FAILED)
        _record_terminal(session, "/media/b.mkv", ended_at=_FIXED_NOW, status=JobStatus.FAILED)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        outcome = scheduler.requeue_all_failed(session=session)

    assert outcome.requeued == []
    assert sorted(outcome.skipped) == ["/media/a.mkv", "/media/b.mkv"]


def test_requeue_all_failed_returns_an_empty_result_when_nothing_is_failed(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        outcome = scheduler.requeue_all_failed(session=session)

    assert outcome.requeued == []
    assert outcome.skipped == []


def test_requeue_all_failed_ignores_non_failed_history_rows(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    ended = _FIXED_NOW - timedelta(hours=7)
    with session_factory() as session:
        _record_terminal(session, "/media/a.mkv", ended_at=ended, status=JobStatus.SUCCEEDED)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        outcome = scheduler.requeue_all_failed(session=session)

    assert outcome.requeued == []
    assert outcome.skipped == []


def test_requeue_all_failed_ignores_a_failed_set_default_audio_row(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A FAILED SET_DEFAULT_AUDIO row is out of scope -- trigger_file only ever

    creates DOWNMIX jobs, so retrying it here would silently substitute the
    wrong kind of job. It belongs to its own bulk endpoint
    (``POST /api/jobs/trigger-default-audio/bulk``, COL-156), not this one --
    so it's excluded entirely rather than surfaced as "skipped".
    """
    ended = _FIXED_NOW - timedelta(hours=7)
    with session_factory() as session:
        job = Job(
            file_path=Path("/media/a.mkv"),
            settings=DownmixSettings(),
            kind=JobKind.SET_DEFAULT_AUDIO,
            status=JobStatus.FAILED,
            started_at=ended,
            ended_at=ended,
        )
        record_job_history(session, job)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        outcome = scheduler.requeue_all_failed(session=session)

    assert outcome.requeued == []
    assert outcome.skipped == []


def test_requeue_all_failed_deduplicates_multiple_failed_rows_for_the_same_file(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A file that has failed more than once has more than one FAILED row, but one retry."""
    ended = _FIXED_NOW - timedelta(hours=7)
    with session_factory() as session:
        job = Job(
            file_path=Path("/media/a.mkv"),
            settings=DownmixSettings(),
            status=JobStatus.FAILED,
            started_at=ended,
            ended_at=ended,
        )
        record_job_history(session, job)
        another = Job(
            file_path=Path("/media/a.mkv"),
            settings=DownmixSettings(),
            status=JobStatus.FAILED,
            started_at=ended,
            ended_at=ended,
        )
        record_job_history(session, another)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        outcome = scheduler.requeue_all_failed(session=session)

    assert len(outcome.requeued) == 1
    assert outcome.requeued[0].file_path == Path("/media/a.mkv")
    assert outcome.skipped == []


def test_requeue_all_failed_still_treats_an_active_job_as_a_duplicate(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A currently-FAILED file with an already-active retry in flight is skipped, not doubled."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    active = scheduler.enqueue_file("/media/a.mkv")
    assert active is not None  # still PENDING -> active

    ended = _FIXED_NOW - timedelta(hours=7)
    with session_factory() as session:
        _record_terminal(session, "/media/a.mkv", ended_at=ended, status=JobStatus.FAILED)

    with session_factory() as session:
        outcome = scheduler.requeue_all_failed(session=session)

    assert outcome.requeued == []
    assert outcome.skipped == ["/media/a.mkv"]


def test_requeue_all_failed_skips_a_file_with_nothing_left_to_do(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A stale FAILED row for a since-fixed file is skipped, not force-requeued."""
    ended = _FIXED_NOW - timedelta(hours=7)
    with session_factory() as session:
        _record_terminal(session, "/media/a.mkv", ended_at=ended, status=JobStatus.FAILED)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_STEREO_ONLY))

    with session_factory() as session:
        outcome = scheduler.requeue_all_failed(session=session)

    assert outcome.requeued == []
    assert outcome.skipped == ["/media/a.mkv"]


def test_requeue_all_failed_is_not_blocked_by_the_auto_queue_limit(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: a bulk requeue that pushes pending above the limit is never blocked either (COL-171)."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    _seed_pending(scheduler, AUTO_QUEUE_LIMIT)

    ended = _FIXED_NOW - timedelta(hours=7)
    with session_factory() as session:
        _record_terminal(session, "/media/extra.mkv", ended_at=ended, status=JobStatus.FAILED)

    with session_factory() as session:
        outcome = scheduler.requeue_all_failed(session=session)

    assert [job.file_path for job in outcome.requeued] == [Path("/media/extra.mkv")]
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT + 1


# ---------------------------------------------------------------------------
# cancel_job (COL-168)
# ---------------------------------------------------------------------------


def test_cancel_job_removes_a_pending_job_and_deletes_its_history(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    job = scheduler.trigger_file("/media/movie.mkv")
    assert job is not None
    with session_factory() as session:
        # _make_scheduler's queue has no history_recorder wired (unlike the
        # production JobQueue.from_settings path) -- seed the row a real
        # history_recorder would already have written on enqueue.
        record_job_history(session, job)

    with session_factory() as session:
        outcome = scheduler.cancel_job(job.id, session=session)

    assert outcome is True
    assert scheduler._queue.get_job(job.id) is None
    with session_factory() as session:
        assert get_job_history(session, job.id) is None


def test_cancel_job_hard_kills_a_running_job(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """COL-192 AC: cancelling a RUNNING Job signals its hard-kill handle and succeeds.

    The killed Job's history row is left intact (unlike the PENDING case's
    delete) -- the worker finishing the killed run re-records it as FAILED
    through the ordinary terminal path.
    """
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    job = scheduler.trigger_file("/media/movie.mkv")
    assert job is not None
    # Simulate a worker having claimed the job and armed its hard-kill handle,
    # exactly as JobQueue._claim_next does the instant it flips it to RUNNING.
    job.status = JobStatus.RUNNING
    job.cancellation = CancellationHandle()
    with session_factory() as session:
        record_job_history(session, job)

    with session_factory() as session:
        outcome = scheduler.cancel_job(job.id, session=session)

    assert outcome is True
    assert job.cancellation.cancelled is True  # the subprocess kill was fired
    # History row is NOT deleted for a running cancel (the worker finalizes it).
    with session_factory() as session:
        assert get_job_history(session, job.id) is not None


def test_cancel_job_reports_too_late_for_a_terminal_job(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A Job that finished naturally between the request and the cancel is a no-op (False)."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    job = scheduler.trigger_file("/media/movie.mkv")
    assert job is not None
    # Simulate the job reaching a terminal status before the cancel request lands.
    job.status = JobStatus.FAILED
    with session_factory() as session:
        record_job_history(session, job)

    with session_factory() as session:
        outcome = scheduler.cancel_job(job.id, session=session)

    assert outcome is False
    # Left exactly as it was: still in the live queue, history row untouched.
    assert scheduler._queue.get_job(job.id) is not None
    with session_factory() as session:
        assert get_job_history(session, job.id) is not None


def test_cancel_job_returns_none_for_an_id_not_in_the_live_queue(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        outcome = scheduler.cancel_job(uuid4(), session=session)

    assert outcome is None


def test_cancel_job_opens_its_own_session_when_none_is_given(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """Mirrors ``_is_duplicate``'s session handling: works without a caller-supplied session."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    job = scheduler.trigger_file("/media/movie.mkv")
    assert job is not None
    with session_factory() as session:
        record_job_history(session, job)

    outcome = scheduler.cancel_job(job.id)

    assert outcome is True
    with session_factory() as session:
        assert get_job_history(session, job.id) is None


# ---------------------------------------------------------------------------
# bump_job_to_front (COL-169)
# ---------------------------------------------------------------------------


def test_bump_job_to_front_reassigns_priority_ahead_of_every_other_pending_job(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    first = scheduler.trigger_file("/media/first.mkv")
    second = scheduler.trigger_file("/media/second.mkv")
    assert first is not None
    assert second is not None

    outcome = scheduler.bump_job_to_front(second.id)

    assert outcome is True
    assert second.priority < first.priority


def test_bump_job_to_front_reports_too_late_for_a_job_no_longer_pending(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    job = scheduler.trigger_file("/media/movie.mkv")
    assert job is not None
    # Simulate a worker having already claimed the job before the bump request lands.
    job.status = JobStatus.RUNNING
    original_priority = job.priority

    outcome = scheduler.bump_job_to_front(job.id)

    assert outcome is False
    # Left exactly as it was: still in the live queue, priority untouched.
    assert scheduler._queue.get_job(job.id) is not None
    assert job.priority == original_priority


def test_bump_job_to_front_returns_none_for_an_id_not_in_the_live_queue(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    outcome = scheduler.bump_job_to_front(uuid4())

    assert outcome is None


def test_bump_job_to_front_repeated_calls_move_each_new_bump_strictly_ahead(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: bump A, then bump B -> B ends up strictly ahead of (would run before) A."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    job_a = scheduler.trigger_file("/media/a.mkv")
    job_b = scheduler.trigger_file("/media/b.mkv")
    assert job_a is not None
    assert job_b is not None

    assert scheduler.bump_job_to_front(job_a.id) is True
    assert scheduler.bump_job_to_front(job_b.id) is True

    assert job_b.priority < job_a.priority


# ---------------------------------------------------------------------------
# process_now / would_exceed_concurrency_limit (COL-229, "Process Now")
# ---------------------------------------------------------------------------


def test_process_now_force_starts_an_already_pending_job(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: Process Now on an already-pending Job immediately starts it running."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    job = scheduler.trigger_file("/media/movie.mkv")
    assert job is not None
    assert job.status is JobStatus.PENDING  # the pool was never started -- still pending

    result = scheduler.process_now("/media/movie.mkv")

    assert result is job
    # Fresh lookups rather than reusing `job.status` again below, since it's
    # re-checked against a different terminal status once the pipeline runs.
    running = scheduler._queue.get_job(job.id)
    assert running is not None
    assert running.status is JobStatus.RUNNING  # flipped synchronously by force_start

    assert scheduler._queue.wait_idle(timeout=5) is True
    finished = scheduler._queue.get_job(job.id)
    assert finished is not None
    assert finished.status is JobStatus.SUCCEEDED


def test_process_now_creates_a_job_when_none_exists_and_force_starts_it(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: Process Now on a Wanted file with no existing Job creates it and starts it."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    assert scheduler._queue.list_jobs() == []

    job = scheduler.process_now("/media/movie.mkv")

    assert job is not None
    assert job.file_path == Path("/media/movie.mkv")
    assert job.status is JobStatus.RUNNING
    assert scheduler._queue.list_jobs() == [job]


def test_process_now_leaves_an_already_running_job_untouched(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Job already RUNNING has nothing to force -- process_now doesn't call force_start again."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    job = scheduler.trigger_file("/media/movie.mkv")
    assert job is not None
    job.status = JobStatus.RUNNING  # simulate a pool worker having already claimed it

    def _fail_if_called(job_id: UUID) -> bool:
        raise AssertionError("force_start must not be called for an already-RUNNING job")

    monkeypatch.setattr(scheduler._queue, "force_start", _fail_if_called)

    result = scheduler.process_now("/media/movie.mkv")

    assert result is job
    assert job.status is JobStatus.RUNNING


def test_process_now_works_identically_whether_auto_processing_pause_is_on_or_off(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: Process Now works identically whether Auto-Processing Pause is on or off."""
    queue = JobQueue(pipeline_runner=_stub_runner(), pause_check=lambda: True)
    scheduler = _make_scheduler(
        settings, session_factory, probe=_probe_returning(_SURROUND), queue=queue
    )
    queue.start()

    job = scheduler.process_now("/media/movie.mkv")

    assert job is not None
    assert job.status is JobStatus.RUNNING  # started despite pause_check always returning True
    assert queue.wait_idle(timeout=5) is True
    finished = queue.get_job(job.id)
    assert finished is not None
    assert finished.status is JobStatus.SUCCEEDED


def test_process_now_bypasses_the_recently_processed_window(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC-adjacent: an explicit Process Now, like any single-file trigger, bypasses the window."""
    with session_factory() as session:
        _record_terminal(session, "/media/movie.mkv", ended_at=_FIXED_NOW)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.process_now("/media/movie.mkv")

    assert job is not None
    assert job.status is JobStatus.RUNNING


def test_process_now_returns_none_when_the_file_has_nothing_to_do(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_STEREO_ONLY))

    assert scheduler.process_now("/media/movie.mkv") is None
    assert scheduler._queue.list_jobs() == []


def test_process_now_returns_none_when_the_file_cannot_be_probed(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_raising())

    assert scheduler.process_now("/media/movie.mkv") is None


def test_process_now_returns_none_when_shutdown_has_already_begun_for_an_existing_pending_job(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """Regression: force_start's new shutdown-no-op must not be reported as success.

    An earlier version of ``process_now`` ignored ``force_start``'s return
    value and always returned the (possibly still-``PENDING``) Job, which
    would make ``POST /api/jobs/process-now`` report ``enqueued=True`` for a
    Job that never actually started.
    """
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    job = scheduler.trigger_file("/media/movie.mkv")
    assert job is not None
    assert job.status is JobStatus.PENDING

    scheduler._queue.shutdown()

    result = scheduler.process_now("/media/movie.mkv")

    assert result is None
    assert job.status is JobStatus.PENDING  # never actually started


def test_process_now_returns_none_when_shutdown_has_already_begun_for_a_newly_created_job(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """Same regression as above, for the "no existing Job yet" creation path."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    scheduler._queue.shutdown()

    result = scheduler.process_now("/media/movie.mkv")

    assert result is None
    created = [j for j in scheduler._queue.list_jobs() if j.file_path == Path("/media/movie.mkv")]
    assert len(created) == 1
    assert created[0].status is JobStatus.PENDING  # created (mirrors enqueue's own post-shutdown
    # behavior), but never actually started -- process_now must not claim otherwise.


def test_process_now_still_reports_success_when_the_pool_wins_the_race_to_claim_it(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """force_start returning False doesn't always mean failure -- the pool may win the race."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    job = scheduler.trigger_file("/media/movie.mkv")
    assert job is not None

    def fake_force_start(job_id: UUID) -> bool:
        # Simulate a pool worker (JobQueue._claim_next) winning the race and
        # claiming this exact Job object -- the same shared reference
        # process_now already holds -- in the moment before force_start
        # itself would have claimed it.
        job.status = JobStatus.RUNNING
        return False

    monkeypatch.setattr(scheduler._queue, "force_start", fake_force_start)

    result = scheduler.process_now("/media/movie.mkv")

    assert result is job
    assert result.status is JobStatus.RUNNING


def test_would_exceed_concurrency_limit_false_when_under_the_limit(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    queue = JobQueue(max_concurrency=2, pipeline_runner=_stub_runner())
    scheduler = _make_scheduler(
        settings, session_factory, probe=_probe_returning(_SURROUND), queue=queue
    )

    assert scheduler.would_exceed_concurrency_limit() is False


def test_would_exceed_concurrency_limit_true_when_running_count_meets_max_concurrency(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: at the Concurrency Limit, starting one more would exceed it."""
    release = threading.Event()

    def blocking_runner(
        file_path: Path, downmix_settings: DownmixSettings, **_: object
    ) -> PipelineResult:
        assert release.wait(timeout=5)
        return _SUCCESS

    queue = JobQueue(max_concurrency=1, pipeline_runner=blocking_runner)
    scheduler = _make_scheduler(
        settings, session_factory, probe=_probe_returning(_SURROUND), queue=queue
    )

    job = scheduler.process_now("/media/movie.mkv")
    assert job is not None
    assert job.status is JobStatus.RUNNING

    assert scheduler.would_exceed_concurrency_limit() is True

    release.set()
    assert queue.wait_idle(timeout=5) is True
    assert scheduler.would_exceed_concurrency_limit() is False  # freed up again once it finished


# ---------------------------------------------------------------------------
# clear_queue (COL-173)
# ---------------------------------------------------------------------------


def test_clear_queue_cancels_every_pending_job_and_deletes_its_history(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    jobs = [scheduler.trigger_file(f"/media/{i}.mkv") for i in range(3)]
    assert all(job is not None for job in jobs)
    with session_factory() as session:
        # _make_scheduler's queue has no history_recorder wired -- seed each
        # PENDING row a real history_recorder would already have written on
        # enqueue, mirroring the cancel_job tests above.
        for job in jobs:
            assert job is not None
            record_job_history(session, job)

    with session_factory() as session:
        result = scheduler.clear_queue(session=session)

    assert result.cancelled == 3
    assert result.already_running == 0
    assert scheduler._queue.list_jobs() == []
    with session_factory() as session:
        for job in jobs:
            assert job is not None
            assert get_job_history(session, job.id) is None


def test_clear_queue_is_a_valid_zero_cancelled_result_for_an_empty_queue(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: clearing an already-empty queue is not an error."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        result = scheduler.clear_queue(session=session)

    assert result.cancelled == 0
    assert result.already_running == 0


def test_clear_queue_ignores_a_job_already_running_before_the_pass_starts(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A Job that's RUNNING before the snapshot is taken is simply never in scope."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    pending_job = scheduler.trigger_file("/media/pending.mkv")
    running_job = scheduler.trigger_file("/media/running.mkv")
    assert pending_job is not None
    assert running_job is not None
    running_job.status = JobStatus.RUNNING  # simulate a worker having already claimed it
    with session_factory() as session:
        record_job_history(session, pending_job)
        record_job_history(session, running_job)

    with session_factory() as session:
        result = scheduler.clear_queue(session=session)

    assert result.cancelled == 1
    assert result.already_running == 0  # never snapshotted -- not "raced," simply out of scope
    assert scheduler._queue.get_job(pending_job.id) is None
    still_there = scheduler._queue.get_job(running_job.id)
    assert still_there is not None
    assert still_there.status is JobStatus.RUNNING


def test_clear_queue_reports_a_job_that_races_to_running_mid_pass(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: some snapshotted jobs may already have started running by the time they're
    individually cancelled -- reported as ``already_running``, left alone, not an error."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    job_a = scheduler.trigger_file("/media/a.mkv")
    job_b = scheduler.trigger_file("/media/b.mkv")
    job_c = scheduler.trigger_file("/media/c.mkv")
    assert job_a is not None
    assert job_b is not None
    assert job_c is not None
    with session_factory() as session:
        for job in (job_a, job_b, job_c):
            record_job_history(session, job)

    # Simulate a worker claiming job_b for real -- flip it PENDING -> RUNNING
    # right in between this pass snapshotting it as PENDING and this pass's
    # own cancel attempt for it (there is no push mechanism to freeze the
    # live queue mid-request, only polling -- the queue's worker pool keeps
    # running concurrently the whole time).
    original_cancel = scheduler._queue.cancel

    def racy_cancel(job_id: UUID) -> bool:
        if job_id == job_b.id:
            job_b.status = JobStatus.RUNNING
        return original_cancel(job_id)

    monkeypatch.setattr(scheduler._queue, "cancel", racy_cancel)

    with session_factory() as session:
        result = scheduler.clear_queue(session=session)

    assert result.cancelled == 2
    assert result.already_running == 1
    # job_a/job_c: genuinely cancelled -- gone from the live queue, no history left.
    assert scheduler._queue.get_job(job_a.id) is None
    assert scheduler._queue.get_job(job_c.id) is None
    with session_factory() as session:
        assert get_job_history(session, job_a.id) is None
        assert get_job_history(session, job_c.id) is None
    # job_b: raced to RUNNING -- left exactly as it was, history row intact.
    still_there = scheduler._queue.get_job(job_b.id)
    assert still_there is not None
    assert still_there.status is JobStatus.RUNNING
    with session_factory() as session:
        assert get_job_history(session, job_b.id) is not None


def test_clear_queue_tops_up_the_auto_queue_limit_exactly_once_after_the_whole_batch(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: after clearing, the Auto-Queue Limit's top-up runs -- pending returns toward the
    limit from Wanted, same as after any other cancellation -- but only once for the whole
    batch, not once per cancellation (which would churn: top up, then immediately re-cancel
    the freshly topped-up job on a later loop iteration)."""
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(
                    instance_id=instance.id, media_title="Show", file_path=f"/tv/wanted{i}.mkv"
                )
                for i in range(AUTO_QUEUE_LIMIT)
            ]
        },
    )
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    seeded = _seed_pending(scheduler, AUTO_QUEUE_LIMIT)
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT

    top_up_calls: list[None] = []
    original_top_up = scheduler.top_up

    def counting_top_up(*, session: Session | None = None) -> list[Job]:
        top_up_calls.append(None)
        return original_top_up(session=session)

    monkeypatch.setattr(scheduler, "top_up", counting_top_up)

    with session_factory() as session:
        result = scheduler.clear_queue(session=session)

    assert result.cancelled == AUTO_QUEUE_LIMIT
    assert result.already_running == 0
    assert len(top_up_calls) == 1  # once for the whole batch, not once per cancellation
    # The pending budget is refilled from Wanted, same as after any other cancellation.
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT
    jobs = scheduler._queue.list_jobs()
    assert all(job.file_path not in {seed.file_path for seed in seeded} for job in jobs)
    assert all(str(job.file_path).startswith("/tv/wanted") for job in jobs)


# ---------------------------------------------------------------------------
# Background loop: triggering + lifecycle.
# ---------------------------------------------------------------------------


def test_start_runs_an_initial_scan_and_drains_the_queue(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting the loop triggers a scan and runs what it enqueues.

    Uses a probe that qualifies only on its first call (COL-171): a Job
    completing now triggers an immediate :meth:`~collapsarr.jobs.scheduler.
    JobScheduler.top_up` pass, and ``fake_fetch`` unconditionally re-reports
    the same single file every call -- mirroring that in a real deployment a
    successful downmix rewrites the file, so the *next* probe correctly finds
    nothing left to do (see the module docstring's Recently-Processed Window
    rationale). Without this, the stub pipeline's no-op "success" plus a
    probe that never changes its answer would make ``top_up`` re-enqueue the
    same file forever.
    """
    _add_instance(session_factory)
    scanned = threading.Event()

    def fake_fetch(inst: ArrInstance, **_: object) -> list[MonitoredFile]:
        scanned.set()
        return [MonitoredFile(instance_id=inst.id, media_title="Show", file_path="/tv/a.mkv")]

    monkeypatch.setattr(scheduler_module, "fetch_monitored_files", fake_fetch)
    queue = JobQueue(pipeline_runner=_stub_runner())
    queue.start()
    probe_calls = {"n": 0}

    def probe_once_then_nothing_to_do(path: Path) -> Sequence[AudioStreamInfo]:
        probe_calls["n"] += 1
        return _SURROUND if probe_calls["n"] == 1 else _STEREO_ONLY

    scheduler = _make_scheduler(
        settings, session_factory, probe=probe_once_then_nothing_to_do, queue=queue
    )

    scheduler.start()
    try:
        assert scanned.wait(timeout=5.0), "background scan did not run"
        _wait_until(lambda: _all_terminal(queue), timeout=5.0)
    finally:
        scheduler.stop()
        queue.shutdown()

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


# ---------------------------------------------------------------------------
# Tracked gate (COL-102): automatic enqueue paths never queue a Not-Tracked
# file; a manual trigger still does.
# ---------------------------------------------------------------------------


def _seed_episode_node(
    session_factory: sessionmaker[Session],
    instance_id: int,
    *,
    episode_id: int = 101,
    tracked: bool | None = None,
) -> None:
    """Upsert one Series>Season>Episode chain, optionally setting a Tracked override."""
    with session_factory() as session:
        node = upsert_series_episode_node(
            session,
            instance_id=instance_id,
            series_id=1,
            series_title="Show",
            season_number=1,
            episode_id=episode_id,
            episode_number=1,
            episode_title="Ep",
        )
        if tracked is not None:
            set_tracked(session, node_id=node.id, tracked=tracked)


def test_enqueue_file_skips_a_not_tracked_file_but_still_tracks_it(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    instance = _add_instance(session_factory)
    _seed_episode_node(session_factory, instance.id, tracked=False)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.enqueue_file("/tv/a.mkv", instance_id=instance.id, sonarr_episode_id=101)

    assert job is None
    assert scheduler._queue.list_jobs() == []
    with session_factory() as session:
        assert get_tracked_media(session, "/tv/a.mkv") is not None  # still tracked/mirrored


def test_enqueue_file_logs_not_tracked_skip_once_per_dedup_window(
    settings: Settings,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """COL-135: repeat scans of the same Not-Tracked file don't spam one log line each."""
    caplog.set_level(logging.INFO, logger="collapsarr.jobs.scheduler")
    instance = _add_instance(session_factory)
    _seed_episode_node(session_factory, instance.id, tracked=False)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    scheduler.enqueue_file("/tv/a.mkv", instance_id=instance.id, sonarr_episode_id=101)
    assert "resolved Not Tracked" in caplog.text

    caplog.clear()
    scheduler.enqueue_file("/tv/a.mkv", instance_id=instance.id, sonarr_episode_id=101)
    assert "resolved Not Tracked" not in caplog.text


def test_enqueue_file_logs_not_tracked_skip_again_after_dedup_window(
    settings: Settings,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """COL-135: the skip line reappears once the dedup window has elapsed."""
    caplog.set_level(logging.INFO, logger="collapsarr.jobs.scheduler")
    instance = _add_instance(session_factory)
    _seed_episode_node(session_factory, instance.id, tracked=False)
    clock = {"now": _FIXED_NOW}
    scheduler = JobScheduler(
        JobQueue(pipeline_runner=_stub_runner()),
        session_factory,
        settings,
        probe=_probe_returning(_SURROUND),
        now=lambda: clock["now"],
    )

    scheduler.enqueue_file("/tv/a.mkv", instance_id=instance.id, sonarr_episode_id=101)
    clock["now"] = _FIXED_NOW + timedelta(
        minutes=DEFAULT_RECENTLY_PROCESSED_WINDOW_MINUTES, seconds=1
    )
    caplog.clear()
    scheduler.enqueue_file("/tv/a.mkv", instance_id=instance.id, sonarr_episode_id=101)

    assert "resolved Not Tracked" in caplog.text


def test_enqueue_file_warns_when_the_bridge_node_is_missing(
    settings: Settings,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """COL-134: ids present but no LibraryNode was ever synced -- bridge can't resolve."""
    caplog.set_level(logging.WARNING, logger="collapsarr.jobs.scheduler")
    instance = _add_instance(session_factory)  # no _seed_episode_node -- no matching node exists
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.enqueue_file("/tv/a.mkv", instance_id=instance.id, sonarr_episode_id=101)

    assert job is not None  # falls back to default_tracked (True), so it still proceeds
    assert "no LibraryNode" in caplog.text


def test_enqueue_file_warns_about_a_missing_bridge_node_once_per_dedup_window(
    settings: Settings,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """COL-134: the bridge-missing warning is rate-limited the same as COL-135's skip log.

    Sets ``default_tracked=False`` so the fallback resolves Not-Tracked and
    the file is skipped rather than enqueued -- otherwise the second call
    would short-circuit on the ordinary queue-dedup check before ever
    reaching the tracked gate this test means to exercise (mirrors how
    COL-135's own rate-limit test needs a Not-Tracked file for the same
    reason).
    """
    caplog.set_level(logging.WARNING, logger="collapsarr.jobs.scheduler")
    instance = _add_instance(session_factory)
    with session_factory() as session:
        update_global_settings(session, default_tracked=False)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.enqueue_file("/tv/a.mkv", instance_id=instance.id, sonarr_episode_id=101)
    assert job is None
    assert "no LibraryNode" in caplog.text

    caplog.clear()
    scheduler.enqueue_file("/tv/a.mkv", instance_id=instance.id, sonarr_episode_id=101)
    assert "no LibraryNode" not in caplog.text


def test_enqueue_file_enqueues_a_tracked_file(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    instance = _add_instance(session_factory)
    _seed_episode_node(session_factory, instance.id, tracked=None)  # inherits default (True)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.enqueue_file("/tv/a.mkv", instance_id=instance.id, sonarr_episode_id=101)

    assert job is not None


def test_enqueue_file_respect_tracked_false_enqueues_a_not_tracked_file(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """The manual-trigger seam: respect_tracked=False bypasses the gate even with ids."""
    instance = _add_instance(session_factory)
    _seed_episode_node(session_factory, instance.id, tracked=False)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.enqueue_file(
        "/tv/a.mkv", instance_id=instance.id, sonarr_episode_id=101, respect_tracked=False
    )

    assert job is not None


def test_trigger_file_enqueues_even_for_a_not_tracked_file(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC6: an explicit manual trigger downmixes a Not-Tracked file."""
    instance = _add_instance(session_factory)
    _seed_episode_node(session_factory, instance.id, tracked=False)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.trigger_file("/tv/a.mkv")

    assert job is not None


def test_enqueue_file_falls_back_to_default_tracked_when_no_node_matches(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """Ids present but no bridged node -> resolve to the global default_tracked."""
    instance = _add_instance(session_factory)  # no library node seeded for episode 101
    with session_factory() as session:
        update_global_settings(session, default_tracked=False)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.enqueue_file("/tv/a.mkv", instance_id=instance.id, sonarr_episode_id=101)

    assert job is None  # default_tracked False -> Not Tracked -> skipped


def test_enqueue_file_with_no_catalog_ids_is_never_gated(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A bare-path enqueue (no instance/episode id) can't be gated -> always proceeds."""
    _add_instance(session_factory)
    with session_factory() as session:
        update_global_settings(session, default_tracked=False)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.enqueue_file("/tv/a.mkv")

    assert job is not None


def test_scan_once_skips_a_not_tracked_file(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3 (scan pass): a Not-Tracked monitored file is mirrored but never enqueued."""
    instance = _add_instance(session_factory)
    _seed_episode_node(session_factory, instance.id, episode_id=101, tracked=False)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(
                    instance_id=instance.id,
                    media_title="Show",
                    file_path="/tv/a.mkv",
                    sonarr_episode_id=101,
                )
            ]
        },
    )
    catalog = SonarrCatalog(
        instance_id=instance.id,
        series=(
            CatalogSeries(
                series_id=1,
                title="Show",
                season_numbers=(1,),
                episodes=(CatalogEpisode(101, 1, 1, "Ep", has_file=True),),
            ),
        ),
    )
    scheduler = JobScheduler(
        JobQueue(pipeline_runner=_stub_runner()),
        session_factory,
        settings,
        probe=_probe_returning(_SURROUND),
        catalog_fetch=lambda _instance: catalog,
        now=lambda: _FIXED_NOW,
    )

    enqueued = scheduler.scan_once()

    assert enqueued == []
    assert scheduler._queue.list_jobs() == []
    with session_factory() as session:
        assert get_tracked_media(session, "/tv/a.mkv") is not None


# ---------------------------------------------------------------------------
# Manual "set default audio track" trigger (COL-155).
# ---------------------------------------------------------------------------

# Two eng streams: 5.1 (not default) and stereo (currently default). With a
# preference of eng/5.1, the 5.1 stream is the exact-match winner but doesn't
# yet carry the disposition -- needs a fix.
_NEEDS_DEFAULT_AUDIO_FIX: list[AudioStreamInfo] = [
    AudioStreamInfo(
        index=0,
        codec="ac3",
        channels=6,
        channel_layout="5.1(side)",
        language="eng",
        is_default=False,
    ),
    AudioStreamInfo(
        index=1, codec="aac", channels=2, channel_layout="stereo", language="eng", is_default=True
    ),
]
# Same streams, but the 5.1 one already carries the disposition -- nothing to do.
_DEFAULT_AUDIO_ALREADY_CORRECT: list[AudioStreamInfo] = [
    AudioStreamInfo(
        index=0,
        codec="ac3",
        channels=6,
        channel_layout="5.1(side)",
        language="eng",
        is_default=True,
    ),
    AudioStreamInfo(
        index=1,
        codec="aac",
        channels=2,
        channel_layout="stereo",
        language="eng",
        is_default=False,
    ),
]
# A single stream -- fewer than two to compare, resolve_default_audio_stream
# returns None regardless of preference.
_SINGLE_STREAM: list[AudioStreamInfo] = [
    AudioStreamInfo(
        index=0, codec="aac", channels=2, channel_layout="stereo", language="eng", is_default=True
    ),
]
# Two different-language 5.1 streams, neither a stereo track yet: qualifies for
# a Stereo downmix job under default settings (for both languages) *and*
# needs a Default Audio Track fix (the eng/5.1 exact-match winner isn't
# currently default) -- used by the shared-dedup tests below, which need one
# fixture both trigger_file and trigger_set_default_audio act on.
_SURROUND_TWO_LANGUAGES_NEEDS_DEFAULT_AUDIO_FIX: list[AudioStreamInfo] = [
    AudioStreamInfo(
        index=0,
        codec="ac3",
        channels=6,
        channel_layout="5.1(side)",
        language="eng",
        is_default=False,
    ),
    AudioStreamInfo(
        index=1,
        codec="ac3",
        channels=6,
        channel_layout="5.1(side)",
        language="jpn",
        is_default=True,
    ),
]

# `ignore_commentary_tracks=True` matches the persisted column's own default
# (COL-244) -- `_configure_preference` below only ever writes
# language/channel_tier, so `job.preference` (re-derived via the real
# `as_default_audio_preference` adapter, COL-250) always carries that default
# too.
_PREFERENCE = DefaultAudioPreference(
    language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE, ignore_commentary_tracks=True
)


def _configure_preference(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        update_global_settings(
            session,
            default_audio_language=_PREFERENCE.language,
            default_audio_channel_tier=_PREFERENCE.channel_tier,
        )


def test_trigger_set_default_audio_enqueues_a_set_default_audio_job_when_a_fix_is_needed(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _configure_preference(session_factory)
    queue = JobQueue(default_audio_pipeline_runner=_stub_default_audio_runner())
    scheduler = _make_scheduler(
        settings, session_factory, probe=_probe_returning(_NEEDS_DEFAULT_AUDIO_FIX), queue=queue
    )

    outcome = scheduler.trigger_set_default_audio("/media/movie.mkv")

    assert outcome.skip_reason is None
    job = outcome.job
    assert job is not None
    assert job.file_path == Path("/media/movie.mkv")
    assert job.kind is JobKind.SET_DEFAULT_AUDIO
    assert job.preference == _PREFERENCE
    assert job.status is JobStatus.PENDING
    assert scheduler._queue.list_jobs() == [job]


def test_trigger_set_default_audio_skips_when_already_correct(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _configure_preference(session_factory)
    queue = JobQueue(default_audio_pipeline_runner=_stub_default_audio_runner())
    scheduler = _make_scheduler(
        settings,
        session_factory,
        probe=_probe_returning(_DEFAULT_AUDIO_ALREADY_CORRECT),
        queue=queue,
    )

    outcome = scheduler.trigger_set_default_audio("/media/movie.mkv")
    assert outcome.job is None
    assert outcome.skip_reason is DefaultAudioSkipReason.ALREADY_CORRECT
    assert scheduler._queue.list_jobs() == []


def test_trigger_set_default_audio_skips_a_file_with_fewer_than_two_streams(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _configure_preference(session_factory)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SINGLE_STREAM))

    outcome = scheduler.trigger_set_default_audio("/media/movie.mkv")
    assert outcome.job is None
    assert outcome.skip_reason is DefaultAudioSkipReason.ALREADY_CORRECT
    assert scheduler._queue.list_jobs() == []


def test_trigger_set_default_audio_skips_when_no_preference_is_configured(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """No default_audio_language/channel_tier set -- nothing to resolve against."""
    scheduler = _make_scheduler(
        settings, session_factory, probe=_probe_returning(_NEEDS_DEFAULT_AUDIO_FIX)
    )

    outcome = scheduler.trigger_set_default_audio("/media/movie.mkv")
    assert outcome.job is None
    assert outcome.skip_reason is DefaultAudioSkipReason.NO_PREFERENCE
    assert scheduler._queue.list_jobs() == []


def test_trigger_set_default_audio_skips_a_file_that_cannot_be_probed(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _configure_preference(session_factory)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_raising())

    outcome = scheduler.trigger_set_default_audio("/media/movie.mkv")
    assert outcome.job is None
    assert outcome.skip_reason is DefaultAudioSkipReason.UNPROBEABLE
    assert scheduler._queue.list_jobs() == []


def test_trigger_set_default_audio_bypasses_the_tracked_gate(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC4: mirrors trigger_file -- a manual trigger acts even if default_tracked is False."""
    _configure_preference(session_factory)
    with session_factory() as session:
        update_global_settings(session, default_tracked=False)
    queue = JobQueue(default_audio_pipeline_runner=_stub_default_audio_runner())
    scheduler = _make_scheduler(
        settings, session_factory, probe=_probe_returning(_NEEDS_DEFAULT_AUDIO_FIX), queue=queue
    )

    outcome = scheduler.trigger_set_default_audio("/media/movie.mkv")

    assert outcome.job is not None


# --- De-duplication spans both job kinds (COL-155's headline risk) ---------


def test_trigger_set_default_audio_is_blocked_by_an_in_flight_downmix_job(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A pending DOWNMIX job for a file blocks a SET_DEFAULT_AUDIO trigger for it."""
    _configure_preference(session_factory)
    queue = JobQueue(
        pipeline_runner=_stub_runner(), default_audio_pipeline_runner=_stub_default_audio_runner()
    )
    scheduler = _make_scheduler(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND_TWO_LANGUAGES_NEEDS_DEFAULT_AUDIO_FIX),
        queue=queue,
    )
    downmix_job = scheduler.trigger_file("/media/movie.mkv")
    assert downmix_job is not None
    assert downmix_job.kind is JobKind.DOWNMIX

    result = scheduler.trigger_set_default_audio("/media/movie.mkv")

    assert result.job is None
    assert result.skip_reason is DefaultAudioSkipReason.DUPLICATE
    assert len(scheduler._queue.list_jobs()) == 1  # only the DOWNMIX job


def test_trigger_file_is_blocked_by_an_in_flight_set_default_audio_job(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """The reverse: a pending SET_DEFAULT_AUDIO job blocks a DOWNMIX trigger for the same file."""
    _configure_preference(session_factory)
    queue = JobQueue(
        pipeline_runner=_stub_runner(), default_audio_pipeline_runner=_stub_default_audio_runner()
    )
    scheduler = _make_scheduler(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND_TWO_LANGUAGES_NEEDS_DEFAULT_AUDIO_FIX),
        queue=queue,
    )
    default_audio_outcome = scheduler.trigger_set_default_audio("/media/movie.mkv")
    assert default_audio_outcome.job is not None
    assert default_audio_outcome.job.kind is JobKind.SET_DEFAULT_AUDIO

    result = scheduler.trigger_file("/media/movie.mkv")

    assert result is None
    assert len(scheduler._queue.list_jobs()) == 1  # only the SET_DEFAULT_AUDIO job


def test_trigger_set_default_audio_re_enqueues_after_a_terminal_downmix_job(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A SUCCEEDED/FAILED job of either kind is not 'active' -- dedup only blocks in-flight jobs."""
    _configure_preference(session_factory)
    queue = JobQueue(
        pipeline_runner=_stub_runner(), default_audio_pipeline_runner=_stub_default_audio_runner()
    )
    scheduler = _make_scheduler(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND_TWO_LANGUAGES_NEEDS_DEFAULT_AUDIO_FIX),
        queue=queue,
    )
    downmix_job = scheduler.trigger_file("/media/movie.mkv")
    assert downmix_job is not None
    downmix_job.status = JobStatus.SUCCEEDED  # in-memory only; no history row persisted

    result = scheduler.trigger_set_default_audio("/media/movie.mkv")

    assert result.job is not None
    assert result.skip_reason is None
    assert len(scheduler._queue.list_jobs()) == 2


# --- bypass_dedup_window (COL-206) ------------------------------------------


def test_trigger_set_default_audio_bypass_dedup_window_ignores_a_recently_processed_history_row(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """``trigger_set_default_audio``'s ``bypass_dedup_window`` mirrors ``trigger_file``'s."""
    _configure_preference(session_factory)
    with session_factory() as session:
        _record_terminal(session, "/media/movie.mkv", ended_at=_FIXED_NOW)

    queue = JobQueue(default_audio_pipeline_runner=_stub_default_audio_runner())
    scheduler = _make_scheduler(
        settings, session_factory, probe=_probe_returning(_NEEDS_DEFAULT_AUDIO_FIX), queue=queue
    )

    with session_factory() as session:
        # Without the bypass this would be skipped (see
        # test_trigger_set_default_audio_defaults_to_respecting_a_recently_processed_history_row).
        outcome = scheduler.trigger_set_default_audio(
            "/media/movie.mkv", session=session, bypass_dedup_window=True
        )
    assert outcome.job is not None
    assert outcome.skip_reason is None


def test_trigger_set_default_audio_bypass_dedup_window_still_treats_an_active_job_as_a_duplicate(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """The bypass only skips the *recently processed* half -- not "already active" (mirrors
    ``test_enqueue_file_bypass_dedup_window_still_treats_an_active_job_as_a_duplicate``).
    """
    _configure_preference(session_factory)
    queue = JobQueue(default_audio_pipeline_runner=_stub_default_audio_runner())
    scheduler = _make_scheduler(
        settings, session_factory, probe=_probe_returning(_NEEDS_DEFAULT_AUDIO_FIX), queue=queue
    )

    first = scheduler.trigger_set_default_audio("/media/movie.mkv")
    assert first.job is not None  # still PENDING -> active

    second = scheduler.trigger_set_default_audio("/media/movie.mkv", bypass_dedup_window=True)

    assert second.job is None
    assert second.skip_reason is DefaultAudioSkipReason.DUPLICATE
    assert len(scheduler._queue.list_jobs()) == 1


def test_trigger_set_default_audio_defaults_to_respecting_a_recently_processed_history_row(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """Without the flag, the window is still respected (unchanged default -- COL-206)."""
    _configure_preference(session_factory)
    with session_factory() as session:
        _record_terminal(session, "/media/movie.mkv", ended_at=_FIXED_NOW)

    queue = JobQueue(default_audio_pipeline_runner=_stub_default_audio_runner())
    scheduler = _make_scheduler(
        settings, session_factory, probe=_probe_returning(_NEEDS_DEFAULT_AUDIO_FIX), queue=queue
    )

    with session_factory() as session:
        outcome = scheduler.trigger_set_default_audio("/media/movie.mkv", session=session)
    assert outcome.job is None
    assert outcome.skip_reason is DefaultAudioSkipReason.DUPLICATE


# ---------------------------------------------------------------------------
# Auto-Queue Limit / top-up (COL-171)
# ---------------------------------------------------------------------------


def _seed_pending(scheduler: JobScheduler, count: int) -> list[Job]:
    """Fill ``scheduler``'s live queue with ``count`` distinct manually-triggered PENDING Jobs.

    Each seeded file is outside any configured instance's Wanted list, so
    ``top_up`` never rediscovers/re-probes it -- these exist purely to
    occupy budget slots.
    """
    jobs: list[Job] = []
    for i in range(count):
        job = scheduler.trigger_file(f"/media/seed{i}.mkv")
        assert job is not None
        jobs.append(job)
    return jobs


def test_top_up_runs_on_job_completion_success(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: a Job completing (success) tops up the budget when pending count is below the limit.

    A probe that qualifies a given path only on its *first* call (mirroring
    a real downmix rewriting the file, so the next probe finds nothing left
    to do -- see the module docstring's Recently-Processed Window rationale)
    keeps this deterministic without needing a ``history_recorder``: once the
    topped-up Wanted file's own Job also completes, ``top_up`` stops
    re-enqueuing it rather than looping forever.
    """
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(
                    instance_id=instance.id, media_title="Show", file_path="/tv/extra.mkv"
                )
            ]
        },
    )
    seen: dict[Path, int] = {}

    def probe_qualifies_once_per_path(path: Path) -> Sequence[AudioStreamInfo]:
        seen[path] = seen.get(path, 0) + 1
        return _SURROUND if seen[path] == 1 else _STEREO_ONLY

    queue = JobQueue(pipeline_runner=_stub_runner())
    scheduler = _make_scheduler(
        settings, session_factory, probe=probe_qualifies_once_per_path, queue=queue
    )

    for i in range(AUTO_QUEUE_LIMIT - 1):
        queue.enqueue(Path(f"/filler{i}.mkv"), DownmixSettings())
    trigger_job = scheduler.trigger_file("/media/trigger.mkv")
    assert trigger_job is not None
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT

    queue.start()
    try:
        assert queue.wait_idle(timeout=5.0)
    finally:
        queue.shutdown()

    jobs = queue.list_jobs()
    assert any(job.file_path == Path("/tv/extra.mkv") for job in jobs)
    assert len(jobs) == AUTO_QUEUE_LIMIT + 1  # every filler + trigger + the topped-up extra
    assert all(job.status is JobStatus.SUCCEEDED for job in jobs)


def test_top_up_runs_on_job_completion_failure(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: a Job completing with a FAILURE also tops up the budget, same as a success."""
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(
                    instance_id=instance.id, media_title="Show", file_path="/tv/extra.mkv"
                )
            ]
        },
    )
    seen: dict[Path, int] = {}

    def probe_qualifies_once_per_path(path: Path) -> Sequence[AudioStreamInfo]:
        seen[path] = seen.get(path, 0) + 1
        return _SURROUND if seen[path] == 1 else _STEREO_ONLY

    queue = JobQueue(pipeline_runner=_stub_runner(_FAILURE))
    scheduler = _make_scheduler(
        settings, session_factory, probe=probe_qualifies_once_per_path, queue=queue
    )

    for i in range(AUTO_QUEUE_LIMIT - 1):
        queue.enqueue(Path(f"/filler{i}.mkv"), DownmixSettings())
    trigger_job = scheduler.trigger_file("/media/trigger.mkv")
    assert trigger_job is not None

    queue.start()
    try:
        assert queue.wait_idle(timeout=5.0)
    finally:
        queue.shutdown()

    jobs = queue.list_jobs()
    assert any(job.file_path == Path("/tv/extra.mkv") for job in jobs)
    assert all(job.status is JobStatus.FAILED for job in jobs)


def test_top_up_runs_on_cancellation(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: cancelling a PENDING Job (COL-168) frees a slot and immediately tops it back up."""
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(
                    instance_id=instance.id, media_title="Show", file_path="/tv/extra.mkv"
                )
            ]
        },
    )
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    seeded = _seed_pending(scheduler, AUTO_QUEUE_LIMIT)
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT

    with session_factory() as session:
        # No history_recorder wired on this queue -- seed the PENDING row
        # cancel_job's history delete expects, mirroring the other cancel_job
        # tests above.
        record_job_history(session, seeded[0])

    with session_factory() as session:
        outcome = scheduler.cancel_job(seeded[0].id, session=session)

    assert outcome is True
    jobs = scheduler._queue.list_jobs()
    assert any(job.file_path == Path("/tv/extra.mkv") for job in jobs)
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT  # the cancelled slot was refilled


def test_trigger_file_is_not_blocked_by_the_auto_queue_limit(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: a manual trigger that pushes pending above the limit is never blocked or rejected."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    _seed_pending(scheduler, AUTO_QUEUE_LIMIT)
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT

    extra = scheduler.trigger_file("/media/one_more.mkv")

    assert extra is not None
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT + 1


def test_requeue_file_is_not_blocked_by_the_auto_queue_limit(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: a requeue (COL-170) that pushes pending above the limit is never blocked either."""
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    _seed_pending(scheduler, AUTO_QUEUE_LIMIT)

    extra = scheduler.requeue_file("/media/one_more.mkv")

    assert extra is not None
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT + 1


def test_scan_now_enqueues_at_most_the_auto_queue_limit(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: ``scan_now`` ("Scan now") enqueues at most the limit, not every qualifying file."""
    instance = _add_instance(session_factory)
    files = [
        MonitoredFile(instance_id=instance.id, media_title="Show", file_path=f"/tv/{i}.mkv")
        for i in range(AUTO_QUEUE_LIMIT + 3)
    ]
    _patch_fetch(monkeypatch, {instance.id: files})
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    enqueued = scheduler.scan_now()

    assert len(enqueued) == AUTO_QUEUE_LIMIT
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT


def test_scan_once_applies_the_same_limit_as_scan_now(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: the periodic background scan (`scan_once`) applies the same limit."""
    instance = _add_instance(session_factory)
    files = [
        MonitoredFile(instance_id=instance.id, media_title="Show", file_path=f"/tv/{i}.mkv")
        for i in range(AUTO_QUEUE_LIMIT + 3)
    ]
    _patch_fetch(monkeypatch, {instance.id: files})
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    enqueued = scheduler.scan_once()

    assert len(enqueued) == AUTO_QUEUE_LIMIT


def test_scan_once_stops_when_wanted_is_exhausted_below_the_limit(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: fewer than the limit's worth of not-yet-queued Wanted entries is a silent no-op."""
    instance = _add_instance(session_factory)
    files = [
        MonitoredFile(instance_id=instance.id, media_title="Show", file_path=f"/tv/{i}.mkv")
        for i in range(AUTO_QUEUE_LIMIT - 2)  # fewer than the limit -- Wanted exhausts first
    ]
    _patch_fetch(monkeypatch, {instance.id: files})
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    enqueued = scheduler.scan_once()

    assert len(enqueued) == AUTO_QUEUE_LIMIT - 2
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT - 2


def test_top_up_is_a_no_op_once_already_at_the_limit(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: the scheduler never auto-enqueues more than the limit's worth of PENDING Jobs."""
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(
                    instance_id=instance.id, media_title="Show", file_path="/tv/extra.mkv"
                )
            ]
        },
    )
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    _seed_pending(scheduler, AUTO_QUEUE_LIMIT)

    result = scheduler.top_up()

    assert result == []
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT


# ---------------------------------------------------------------------------
# Auto-Queuing Pause (COL-174).
# ---------------------------------------------------------------------------


def test_scan_once_does_not_auto_enqueue_when_paused(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: when set, the periodic scan's auto-enqueue-from-Wanted does not run."""
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(instance_id=instance.id, media_title="Show", file_path="/tv/a.mkv")
            ]
        },
    )
    with session_factory() as session:
        update_global_settings(session, auto_queue_paused=True)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    enqueued = scheduler.scan_once()

    assert enqueued == []
    assert scheduler._queue.list_jobs() == []


def test_scan_now_does_not_auto_enqueue_when_paused(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: "Scan now" is an alias for scan_once, so it inherits the pause too."""
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(instance_id=instance.id, media_title="Show", file_path="/tv/a.mkv")
            ]
        },
    )
    with session_factory() as session:
        update_global_settings(session, auto_queue_paused=True)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    enqueued = scheduler.scan_now()

    assert enqueued == []
    assert scheduler._queue.list_jobs() == []


def test_top_up_is_a_no_op_when_paused_even_with_qualifying_wanted_entries(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct ``top_up()`` call is also a no-op while paused, not just the scan wrappers."""
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(instance_id=instance.id, media_title="Show", file_path="/tv/a.mkv")
            ]
        },
    )
    with session_factory() as session:
        update_global_settings(session, auto_queue_paused=True)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    result = scheduler.top_up()

    assert result == []
    assert scheduler._count_pending() == 0


def test_top_up_does_not_run_on_job_completion_when_paused(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: the Auto-Queue Limit's completion-triggered top-up does not run while paused."""
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(
                    instance_id=instance.id, media_title="Show", file_path="/tv/extra.mkv"
                )
            ]
        },
    )
    with session_factory() as session:
        update_global_settings(session, auto_queue_paused=True)

    queue = JobQueue(pipeline_runner=_stub_runner())
    scheduler = _make_scheduler(
        settings, session_factory, probe=_probe_returning(_SURROUND), queue=queue
    )
    # A manual trigger still works while paused (see below) -- used here purely
    # to give the queue something to complete and fire the terminal hook.
    trigger_job = scheduler.trigger_file("/media/trigger.mkv")
    assert trigger_job is not None

    queue.start()
    try:
        assert queue.wait_idle(timeout=5.0)
    finally:
        queue.shutdown()

    jobs = queue.list_jobs()
    assert len(jobs) == 1  # only the manual trigger -- no top-up-discovered extra
    assert not any(job.file_path == Path("/tv/extra.mkv") for job in jobs)


def test_top_up_does_not_run_on_cancellation_when_paused(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: the Auto-Queue Limit's cancellation-triggered top-up does not run while paused."""
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(
                    instance_id=instance.id, media_title="Show", file_path="/tv/extra.mkv"
                )
            ]
        },
    )
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))
    seeded = _seed_pending(scheduler, AUTO_QUEUE_LIMIT)
    with session_factory() as session:
        # No history_recorder wired on this queue -- seed the PENDING row
        # cancel_job's history delete expects, mirroring the other cancel_job
        # tests above.
        record_job_history(session, seeded[0])
        update_global_settings(session, auto_queue_paused=True)

    with session_factory() as session:
        outcome = scheduler.cancel_job(seeded[0].id, session=session)

    assert outcome is True
    jobs = scheduler._queue.list_jobs()
    assert not any(job.file_path == Path("/tv/extra.mkv") for job in jobs)
    assert scheduler._count_pending() == AUTO_QUEUE_LIMIT - 1  # the freed slot stayed empty


def test_pending_and_running_jobs_continue_to_completion_while_paused(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: pausing auto-fill never stops an already-PENDING/RUNNING Job from finishing."""
    queue = JobQueue(pipeline_runner=_stub_runner())
    scheduler = _make_scheduler(
        settings, session_factory, probe=_probe_returning(_SURROUND), queue=queue
    )
    job = scheduler.trigger_file("/media/already-queued.mkv")
    assert job is not None

    with session_factory() as session:
        update_global_settings(session, auto_queue_paused=True)

    queue.start()
    try:
        assert queue.wait_idle(timeout=5.0)
    finally:
        queue.shutdown()

    jobs = queue.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].status is JobStatus.SUCCEEDED


def test_trigger_file_still_works_while_paused(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: manual single-file trigger keeps working while auto-fill is paused."""
    with session_factory() as session:
        update_global_settings(session, auto_queue_paused=True)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.trigger_file("/media/movie.mkv")

    assert job is not None


def test_requeue_file_still_works_while_paused(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: manual single-file requeue keeps working while auto-fill is paused."""
    with session_factory() as session:
        update_global_settings(session, auto_queue_paused=True)
        _record_terminal(session, "/media/movie.mkv", ended_at=_FIXED_NOW, status=JobStatus.FAILED)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    job = scheduler.requeue_file("/media/movie.mkv")

    assert job is not None


def test_requeue_all_failed_still_works_while_paused(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """AC: bulk 'Requeue all failed' keeps working while auto-fill is paused."""
    ended = _FIXED_NOW - timedelta(hours=7)  # outside the default 360min window
    with session_factory() as session:
        update_global_settings(session, auto_queue_paused=True)
        _record_terminal(session, "/media/a.mkv", ended_at=ended, status=JobStatus.FAILED)
    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    with session_factory() as session:
        outcome = scheduler.requeue_all_failed(session=session)

    assert {job.file_path for job in outcome.requeued} == {Path("/media/a.mkv")}
    assert outcome.skipped == []


def test_auto_queue_paused_survives_a_fresh_scheduler_construction(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: the value survives a process restart.

    A brand-new :class:`JobScheduler` instance -- carrying no in-memory state
    from any prior one -- built against a database where the flag is already
    set stays paused, since the check reads the persisted row live rather
    than a value cached at construction.
    """
    instance = _add_instance(session_factory)
    _patch_fetch(
        monkeypatch,
        {
            instance.id: [
                MonitoredFile(instance_id=instance.id, media_title="Show", file_path="/tv/a.mkv")
            ]
        },
    )
    with session_factory() as session:
        update_global_settings(session, auto_queue_paused=True)

    scheduler = _make_scheduler(settings, session_factory, probe=_probe_returning(_SURROUND))

    enqueued = scheduler.scan_once()

    assert enqueued == []
    assert scheduler._queue.list_jobs() == []
