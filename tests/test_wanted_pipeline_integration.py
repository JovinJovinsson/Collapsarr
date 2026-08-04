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

from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.config import Settings
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget, QualifyingTarget
from collapsarr.jobs.queue import JobQueue, JobStatus, PipelineRunner
from collapsarr.jobs.scheduler import JobScheduler, ProbeFn
from collapsarr.jobs.tracked_media import make_tracked_media_recorder
from collapsarr.media.service import get_tracked_media, list_files_missing_targets
from collapsarr.settings.service import get_global_settings

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

    ran = queue.run_pending()

    assert ran[0].status is JobStatus.SUCCEEDED
    # No further scan/enqueue_file call happens here -- the job-completion
    # hook alone must be what clears the file from the wanted-list.
    assert _wanted_paths(client) == set()
