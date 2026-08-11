"""End-to-end integration test for the scan/job -> tracked-media -> Wanted chain (COL-95).

Every existing scheduler/queue/media-service test exercises one piece in
isolation -- scheduler tests stub the probe *and* the queue's pipeline
runner, queue tests stub the pipeline runner and never touch tracked media,
media-service tests seed the tracked-media table directly, and Wanted-route
tests seed it directly too. None of them proves the pieces are actually
wired together in the real app.

This module drives the real chain end to end: a real
:class:`~collapsarr.jobs.scheduler.JobScheduler` enqueuing onto a real
:class:`~collapsarr.jobs.queue.JobQueue`, with its real
``tracked_media_recorder`` (:func:`~collapsarr.jobs.tracked_media.
make_tracked_media_recorder`) wired up -- exactly as
:meth:`~collapsarr.jobs.queue.JobQueue.from_settings` wires it in
production. The only stubs are at the ffmpeg/ffprobe boundary: a ``probe``
function standing in for :func:`~collapsarr.downmix.probe.probe_audio_streams`,
and a ``pipeline_runner`` standing in for
:func:`~collapsarr.downmix.pipeline.run_downmix_pipeline` (already covered
end-to-end against real binaries elsewhere, e.g.
``tests/test_downmix_pipeline.py``). Assertions read the real
:func:`~collapsarr.media.service.list_files_missing_targets` query and the
real ``GET /api/wanted`` HTTP endpoint -- the two things this ticket's
acceptance criteria are stated in terms of -- rather than inspecting mocks.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.arr.models import ArrInstance, InstanceType
from collapsarr.config import Settings
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget, QualifyingTarget
from collapsarr.jobs.queue import JobQueue, JobStatus, PipelineRunner
from collapsarr.jobs.scheduler import JobScheduler, ProbeFn
from collapsarr.jobs.tracked_media import make_tracked_media_recorder
from collapsarr.library.service import (
    get_node_by_source_id,
    set_tracked,
    upsert_series_episode_node,
)
from collapsarr.media.models import MediaTargetStatus
from collapsarr.media.service import (
    get_tracked_media,
    list_files_missing_targets,
    list_target_statuses,
)
from collapsarr.settings.service import get_global_settings

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arr"

# A 5.1 stream: with the default (Stereo-only) settings, Stereo (2ch < 6ch,
# not present) qualifies as a missing target.
_SURROUND: list[AudioStreamInfo] = [
    AudioStreamInfo(index=0, codec="ac3", channels=6, channel_layout="5.1(side)", language="eng")
]
# A stereo-only stream: Stereo is already present at 2ch -> nothing qualifies,
# the file is fully processed.
_STEREO_ONLY: list[AudioStreamInfo] = [
    AudioStreamInfo(index=0, codec="aac", channels=2, channel_layout="stereo", language="eng")
]

_NOTHING_TO_DO = PipelineResult(
    outcome=PipelineOutcome.NOTHING_TO_DO, success=True, detail="nothing to do"
)


def _probe_returning(streams: Sequence[AudioStreamInfo]) -> ProbeFn:
    def probe(path: Path) -> Sequence[AudioStreamInfo]:
        return streams

    return probe


def _pipeline_runner_returning(result: PipelineResult) -> PipelineRunner:
    """A pipeline_runner stub returning a fixed result, for jobs that never actually run."""

    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        return result

    return runner


def _pipeline_stub_adding(*targets: QualifyingTarget) -> PipelineRunner:
    """A pipeline_runner stand-in for the ffmpeg-backed pipeline (COL-19).

    Reports a successful downmix that added exactly ``targets`` -- the
    ``tracks_added`` shape the real pipeline reports on success, which is
    what :func:`~collapsarr.jobs.tracked_media.record_tracked_media` reads to
    decide which ``(language, target)`` pairs to flip to ``PROCESSED``.
    """

    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        return PipelineResult(
            outcome=PipelineOutcome.SUCCESS,
            success=True,
            detail="ok",
            tracks_added=targets,
        )

    return runner


def _wire(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    probe: ProbeFn,
    pipeline_runner: PipelineRunner,
) -> tuple[JobScheduler, JobQueue]:
    """Wire a real scheduler + queue together the way production does.

    ``tracked_media_recorder`` is the real
    :func:`~collapsarr.jobs.tracked_media.make_tracked_media_recorder`
    bound to ``session_factory`` -- the same constructor
    :meth:`~collapsarr.jobs.queue.JobQueue.from_settings` defaults to in
    production. Only ``probe`` and ``pipeline_runner`` (the ffmpeg/ffprobe
    boundary) are stubs.
    """
    queue = JobQueue(
        pipeline_runner=pipeline_runner,
        tracked_media_recorder=make_tracked_media_recorder(session_factory),
    )
    scheduler = JobScheduler(queue, session_factory, settings, probe=probe)
    return scheduler, queue


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _wanted_paths(client: TestClient) -> set[str]:
    response = client.get("/api/wanted", headers=_auth_headers(client))
    assert response.status_code == 200, response.text
    return {row["file_path"] for row in response.json()}


# ---------------------------------------------------------------------------
# AC1: a scan/webhook discovering a missing target -> appears in /api/wanted.
# ---------------------------------------------------------------------------


def test_a_discovered_file_with_a_missing_target_appears_in_wanted(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    scheduler, _queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_runner_returning(_NOTHING_TO_DO),
    )

    job = scheduler.enqueue_file("/media/movie.mkv")

    assert job is not None
    with session_factory() as session:
        missing = list_files_missing_targets(
            session, enabled_targets=frozenset({DownmixTarget.STEREO})
        )
    assert [f.file_path for f in missing] == ["/media/movie.mkv"]
    assert _wanted_paths(client) == {"/media/movie.mkv"}


# ---------------------------------------------------------------------------
# AC2: a scan discovering a file with every target already present -> not
# wanted, and correctly PROCESSED rather than left untracked entirely.
# ---------------------------------------------------------------------------


def test_a_discovered_fully_processed_file_does_not_appear_in_wanted(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    scheduler, _queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_STEREO_ONLY),
        pipeline_runner=_pipeline_runner_returning(_NOTHING_TO_DO),
    )

    job = scheduler.enqueue_file("/media/already-stereo.mkv")

    assert job is None  # nothing qualifies -- no job enqueued
    # But the file must still be *tracked*, correctly as fully processed --
    # not left out of the tracked-media table entirely.
    with session_factory() as session:
        media = get_tracked_media(session, "/media/already-stereo.mkv")
        assert media is not None
    assert _wanted_paths(client) == set()


# ---------------------------------------------------------------------------
# AC3: a downmix job succeeding flips its now-satisfied target so the file
# stops appearing in /api/wanted immediately -- no rescan needed.
# ---------------------------------------------------------------------------


def test_a_successful_downmix_job_removes_the_file_from_wanted_without_a_rescan(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    added = QualifyingTarget(language="eng", target=DownmixTarget.STEREO)
    scheduler, queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_stub_adding(added),
    )

    job = scheduler.enqueue_file("/media/movie.mkv")
    assert job is not None
    # Before the job runs, the file is wanted (AC1's scenario).
    assert _wanted_paths(client) == {"/media/movie.mkv"}

    queue.start()
    queue.wait_idle()

    assert job.status is JobStatus.SUCCEEDED
    # No further scan/enqueue_file call happens here -- the job-completion
    # hook alone must be what clears the file from the wanted-list.
    assert _wanted_paths(client) == set()


# ---------------------------------------------------------------------------
# COL-102: the Tracked flag governs the live auto-enqueue + Wanted pipeline,
# and webhooks keep the Library mirror current. These extend the pattern above
# (real scheduler + queue + tracked_media_recorder, stubbing only the
# ffmpeg/ffprobe boundary) with a seeded ArrInstance + Library node so a file's
# resolved Tracked value is exercised end to end.
# ---------------------------------------------------------------------------

_IMPORT_PATH = "/tv/Breaking Bad/Season 01/Breaking Bad - S01E01 - Pilot.mkv"


def _sonarr_import_payload() -> dict[str, object]:
    payload = json.loads((_FIXTURES_DIR / "sonarr_webhook_on_import.json").read_text())
    assert isinstance(payload, dict)
    return payload


def _seed_sonarr_episode(
    session_factory: sessionmaker[Session],
    *,
    tracked: bool | None,
    episode_id: int = 101,
) -> int:
    """Seed a Sonarr instance + one Library episode; return the instance id.

    ``tracked`` sets an explicit override on the series (cascading to the
    episode); ``None`` leaves it inheriting the global default (Tracked).
    """
    with session_factory() as session:
        instance = ArrInstance(
            name="Sonarr", type=InstanceType.SONARR, base_url="http://sonarr.local", api_key="k"
        )
        session.add(instance)
        session.commit()
        session.refresh(instance)
        node = upsert_series_episode_node(
            session,
            instance_id=instance.id,
            series_id=1,
            series_title="Breaking Bad",
            season_number=1,
            episode_id=episode_id,
            episode_number=1,
            episode_title="Pilot",
        )
        if tracked is not None:
            set_tracked(session, node_id=node.id, tracked=tracked)
        return instance.id


def _seed_bare_sonarr_instance(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        instance = ArrInstance(
            name="Sonarr", type=InstanceType.SONARR, base_url="http://sonarr.local", api_key="k"
        )
        session.add(instance)
        session.commit()
        session.refresh(instance)
        return instance.id


# --- AC2 + AC3: a Not-Tracked discovered file is neither enqueued nor Wanted ---


def test_a_not_tracked_discovered_file_is_neither_enqueued_nor_wanted(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_sonarr_episode(session_factory, tracked=False)
    scheduler, _queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_runner_returning(_NOTHING_TO_DO),
    )

    job = scheduler.enqueue_file(
        "/media/pilot.mkv", instance_id=instance_id, sonarr_episode_id=101
    )

    assert job is None  # AC3: automatic enqueue is gated by Tracked
    # AC2: the file is still tracked (a MISSING row exists) but excluded from Wanted.
    with session_factory() as session:
        assert get_tracked_media(session, "/media/pilot.mkv") is not None
    assert _wanted_paths(client) == set()


def test_a_tracked_discovered_file_is_enqueued_and_wanted(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_sonarr_episode(session_factory, tracked=None)  # inherits default (True)
    scheduler, _queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_runner_returning(_NOTHING_TO_DO),
    )

    job = scheduler.enqueue_file(
        "/media/pilot.mkv", instance_id=instance_id, sonarr_episode_id=101
    )

    assert job is not None
    assert _wanted_paths(client) == {"/media/pilot.mkv"}


# --- AC4: flipping Not-Tracked never cancels an already-queued/running job ------


def test_flipping_not_tracked_does_not_cancel_an_already_queued_job(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_sonarr_episode(session_factory, tracked=None)
    added = QualifyingTarget(language="eng", target=DownmixTarget.STEREO)
    scheduler, queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_stub_adding(added),
    )

    job = scheduler.enqueue_file(
        "/media/pilot.mkv", instance_id=instance_id, sonarr_episode_id=101
    )
    assert job is not None
    assert job.status is JobStatus.PENDING

    # The user flips the episode Not-Tracked while its job sits queued.
    with session_factory() as session:
        node = get_node_by_source_id(session, instance_id=instance_id, sonarr_episode_id=101)
        assert node is not None
        set_tracked(session, node_id=node.id, tracked=False)

    # The already-queued job is untouched -- not cancelled/removed -- and still
    # runs to completion. Tracked only gates *future* automatic enqueueing.
    assert queue.get_job(job.id) is not None
    assert queue.get_job(job.id).status is JobStatus.PENDING  # type: ignore[union-attr]
    queue.start()
    queue.wait_idle()
    assert [j.id for j in queue.list_jobs()] == [job.id]
    assert queue.get_job(job.id).status is JobStatus.SUCCEEDED  # type: ignore[union-attr]


def test_flipping_not_tracked_does_not_cancel_a_running_job(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_sonarr_episode(session_factory, tracked=None)
    scheduler, queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_runner_returning(_NOTHING_TO_DO),
    )

    job = scheduler.enqueue_file(
        "/media/pilot.mkv", instance_id=instance_id, sonarr_episode_id=101
    )
    assert job is not None
    job.status = JobStatus.RUNNING  # simulate a worker having picked it up

    with session_factory() as session:
        node = get_node_by_source_id(session, instance_id=instance_id, sonarr_episode_id=101)
        assert node is not None
        set_tracked(session, node_id=node.id, tracked=False)

    # The in-flight job is neither removed from the queue nor reverted.
    assert queue.get_job(job.id) is job
    assert queue.get_job(job.id).status is JobStatus.RUNNING  # type: ignore[union-attr]


# --- AC5: already-produced downmix tracks survive a flip to Not-Tracked ---------


def test_flipping_not_tracked_leaves_already_produced_tracks_untouched(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_sonarr_episode(session_factory, tracked=None)
    added = QualifyingTarget(language="eng", target=DownmixTarget.STEREO)
    scheduler, queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_stub_adding(added),
    )

    scheduler.enqueue_file("/media/pilot.mkv", instance_id=instance_id, sonarr_episode_id=101)
    queue.start()
    queue.wait_idle()

    with session_factory() as session:
        produced_before = {
            s.target
            for s in list_target_statuses(session, "/media/pilot.mkv")
            if s.status is MediaTargetStatus.PROCESSED
        }
    assert DownmixTarget.STEREO in produced_before  # the job added the stereo track

    with session_factory() as session:
        node = get_node_by_source_id(session, instance_id=instance_id, sonarr_episode_id=101)
        assert node is not None
        set_tracked(session, node_id=node.id, tracked=False)

    # Flipping Not-Tracked must not retroactively remove/alter produced tracks.
    with session_factory() as session:
        produced_after = {
            s.target
            for s in list_target_statuses(session, "/media/pilot.mkv")
            if s.status is MediaTargetStatus.PROCESSED
        }
    assert produced_after == produced_before


# --- AC6: the manual trigger endpoint still works on a Not-Tracked file ---------


def test_manual_trigger_endpoint_downmixes_a_not_tracked_file(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    _seed_sonarr_episode(session_factory, tracked=False)
    scheduler, _queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_runner_returning(_NOTHING_TO_DO),
    )
    # Wire the scheduler so POST /api/jobs/trigger resolves it (the client
    # fixture builds the app without enable_scheduler).
    app.state.job_scheduler = scheduler

    response = client.post(
        "/api/jobs/trigger",
        json={"file_path": "/media/pilot.mkv"},
        headers=_auth_headers(client),
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["enqueued"] is True
    assert body["job"]["file_path"] == "/media/pilot.mkv"


# --- AC1 + AC7: the webhook path, end to end through the real HTTP API ----------


def test_webhook_import_upserts_the_node_and_a_tracked_file_reaches_wanted(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_bare_sonarr_instance(session_factory)
    scheduler, _queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_runner_returning(_NOTHING_TO_DO),
    )
    # Override the app's file-ready hook so the webhook auto-enqueues for real.
    app.state.on_file_ready = scheduler.on_file_ready

    response = client.post(
        f"/api/webhook/arr/{instance_id}",
        json=_sonarr_import_payload(),
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    # AC1: the import upserted the Library node(s) via the real HTTP webhook.
    with session_factory() as session:
        node = get_node_by_source_id(session, instance_id=instance_id, sonarr_episode_id=101)
        assert node is not None
    # AC7: Tracked by default -> the file flows through to /api/wanted.
    assert _wanted_paths(client) == {_IMPORT_PATH}


def test_webhook_import_of_a_not_tracked_episode_is_not_enqueued_or_wanted(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_sonarr_episode(session_factory, tracked=False, episode_id=101)
    scheduler, queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_runner_returning(_NOTHING_TO_DO),
    )
    app.state.on_file_ready = scheduler.on_file_ready

    response = client.post(
        f"/api/webhook/arr/{instance_id}",
        json=_sonarr_import_payload(),
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    assert queue.list_jobs() == []  # AC3: the webhook pass did not enqueue
    assert _wanted_paths(client) == set()  # AC2


# ---------------------------------------------------------------------------
# COL-97: flipping an already-Wanted file's node to Not-Tracked must remove it
# from /api/wanted immediately, without a rescan -- exercised at every layer
# a real "untrack" action can come from (direct service call, a Series-level
# cascade onto an already-discovered episode, and the actual HTTP endpoint the
# frontend calls).
# ---------------------------------------------------------------------------


def test_untracking_an_already_wanted_file_removes_it_from_wanted(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_sonarr_episode(session_factory, tracked=None)  # inherits default (True)
    scheduler, _queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_runner_returning(_NOTHING_TO_DO),
    )

    job = scheduler.enqueue_file(
        _IMPORT_PATH, instance_id=instance_id, sonarr_episode_id=101
    )
    assert job is not None
    assert _wanted_paths(client) == {_IMPORT_PATH}

    with session_factory() as session:
        node = get_node_by_source_id(session, instance_id=instance_id, sonarr_episode_id=101)
        assert node is not None
        set_tracked(session, node_id=node.id, tracked=False)

    assert _wanted_paths(client) == set()


def test_untracking_a_series_cascades_and_removes_its_wanted_episode(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_sonarr_episode(session_factory, tracked=None)
    scheduler, _queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_runner_returning(_NOTHING_TO_DO),
    )

    job = scheduler.enqueue_file(
        _IMPORT_PATH, instance_id=instance_id, sonarr_episode_id=101
    )
    assert job is not None
    assert _wanted_paths(client) == {_IMPORT_PATH}

    with session_factory() as session:
        episode_node = get_node_by_source_id(
            session, instance_id=instance_id, sonarr_episode_id=101
        )
        assert episode_node is not None
        season_node = session.get(type(episode_node), episode_node.parent_id)
        assert season_node is not None
        set_tracked(session, node_id=season_node.parent_id, tracked=False)  # the series

    assert _wanted_paths(client) == set()


def test_untracking_via_the_http_endpoint_removes_it_from_wanted(
    settings: Settings, client: TestClient
) -> None:
    """Exercises the real path the frontend's `TrackedToggleButton` drives."""
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_sonarr_episode(session_factory, tracked=None)
    scheduler, _queue = _wire(
        settings,
        session_factory,
        probe=_probe_returning(_SURROUND),
        pipeline_runner=_pipeline_runner_returning(_NOTHING_TO_DO),
    )

    job = scheduler.enqueue_file(
        _IMPORT_PATH, instance_id=instance_id, sonarr_episode_id=101
    )
    assert job is not None
    assert _wanted_paths(client) == {_IMPORT_PATH}

    with session_factory() as session:
        node = get_node_by_source_id(session, instance_id=instance_id, sonarr_episode_id=101)
        assert node is not None
        node_id = node.id

    response = client.post(
        "/api/library/tracked",
        json={"references": [{"node_type": "episode", "node_id": node_id}], "tracked": False},
        headers=_auth_headers(client),
    )
    assert response.status_code == 200, response.text

    assert _wanted_paths(client) == set()
