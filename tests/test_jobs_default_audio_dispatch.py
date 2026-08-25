"""Tests for the Default Audio Track Job mechanism-selection gate (COL-247).

Every case drives :func:`~collapsarr.jobs.default_audio_dispatch.
make_default_audio_pipeline_runner` directly, with recording fakes standing
in for both mechanisms (:func:`~collapsarr.plex.default_audio_write.
apply_default_audio_via_plex` and :func:`~collapsarr.downmix.
default_audio_pipeline.run_default_audio_pipeline`) -- no live Plex server or
ffmpeg/ffprobe binary required. This is the "equivalent seam at this
integration point" COL-247's acceptance criteria call for, mirroring
``test_jobs_plex_analyze.py``'s pattern for its own hook.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.downmix.default_audio import DefaultAudioPreference
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.targets import DownmixTarget
from collapsarr.jobs.default_audio_dispatch import make_default_audio_pipeline_runner
from collapsarr.jobs.queue import DefaultAudioPipelineRunner, JobQueue, JobStatus
from collapsarr.migrations import upgrade_to_head
from collapsarr.plex.default_audio_write import PlexDefaultAudioOutcome, PlexDefaultAudioResult
from collapsarr.plex.service import update_plex_connection

_PREFERENCE = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE)
BASE_URL = "http://plex.local:32400"
TOKEN = "plex-token"
FILE_PATH = "/media/movie.mkv"

_REMUX_SUCCESS = PipelineResult(outcome=PipelineOutcome.SUCCESS, success=True, detail="remuxed")
_PLEX_SUCCESS = PlexDefaultAudioResult(
    outcome=PlexDefaultAudioOutcome.SUCCESS,
    success=True,
    detail="set via plex",
    rating_key="123",
    stream_id="7",
)


@pytest.fixture
def session_factory(settings: Settings) -> Iterator[sessionmaker[Session]]:
    """A schema-initialised session factory over the isolated ``settings`` DB."""
    engine = create_engine_from_settings(settings)
    upgrade_to_head(settings)
    yield create_session_factory(engine)
    engine.dispose()


class _StubPlexRunner:
    """A ``plex_runner`` stub recording every call and returning a fixed result."""

    def __init__(self, result: PlexDefaultAudioResult) -> None:
        self._result = result
        self.calls: list[tuple[Session, str, DefaultAudioPreference, str, str]] = []

    def __call__(
        self,
        session: Session,
        file_path: str | Path,
        preference: DefaultAudioPreference,
        *,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> PlexDefaultAudioResult:
        self.calls.append((session, str(file_path), preference, base_url, token))
        return self._result


class _StubRemuxRunner:
    """A ``remux_runner`` stub recording every call and returning a fixed result."""

    def __init__(self, result: PipelineResult) -> None:
        self._result = result
        self.calls: list[tuple[str, DefaultAudioPreference, dict[str, Any]]] = []

    def __call__(
        self, file_path: str | Path, preference: DefaultAudioPreference, **kwargs: object
    ) -> PipelineResult:
        self.calls.append((str(file_path), preference, dict(kwargs)))
        return self._result


def _configure_plex(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        update_plex_connection(session, base_url=BASE_URL, token=TOKEN)


# ---------------------------------------------------------------------------
# Core gate: exactly one mechanism runs, based on PlexConnection.is_configured.
# ---------------------------------------------------------------------------


def test_configured_plex_connection_routes_to_the_plex_runner_only(
    session_factory: sessionmaker[Session],
) -> None:
    _configure_plex(session_factory)
    plex_runner = _StubPlexRunner(_PLEX_SUCCESS)
    remux_runner = _StubRemuxRunner(_REMUX_SUCCESS)
    runner = make_default_audio_pipeline_runner(
        session_factory, plex_runner=plex_runner, remux_runner=remux_runner
    )

    result = runner(FILE_PATH, _PREFERENCE)

    assert len(plex_runner.calls) == 1
    assert remux_runner.calls == []
    _, called_path, called_preference, base_url, token = plex_runner.calls[0]
    assert called_path == FILE_PATH
    assert called_preference == _PREFERENCE
    assert base_url == BASE_URL
    assert token == TOKEN
    assert result.success is True


def test_unconfigured_plex_connection_routes_to_the_remux_runner_only(
    session_factory: sessionmaker[Session],
) -> None:
    """No PlexConnection ever saved -- the default, freshly-created connection row."""
    plex_runner = _StubPlexRunner(_PLEX_SUCCESS)
    remux_runner = _StubRemuxRunner(_REMUX_SUCCESS)
    runner = make_default_audio_pipeline_runner(
        session_factory, plex_runner=plex_runner, remux_runner=remux_runner
    )

    result = runner(FILE_PATH, _PREFERENCE)

    assert remux_runner.calls == [(FILE_PATH, _PREFERENCE, {})]
    assert plex_runner.calls == []
    assert result is _REMUX_SUCCESS


def test_remux_runner_kwargs_are_forwarded_unchanged(
    session_factory: sessionmaker[Session],
) -> None:
    """``cancel_handle``/``ffmpeg_path`` (JobQueue._run_job's forwarded kwargs) reach the remux
    path."""
    remux_runner = _StubRemuxRunner(_REMUX_SUCCESS)
    runner = make_default_audio_pipeline_runner(session_factory, remux_runner=remux_runner)

    runner(FILE_PATH, _PREFERENCE, cancel_handle=None, ffmpeg_path="/opt/ffmpeg/ffmpeg")

    assert remux_runner.calls == [
        (FILE_PATH, _PREFERENCE, {"cancel_handle": None, "ffmpeg_path": "/opt/ffmpeg/ffmpeg"})
    ]


# ---------------------------------------------------------------------------
# Result adaptation: PlexDefaultAudioResult -> PipelineResult.
# ---------------------------------------------------------------------------


def test_plex_success_outcome_maps_to_pipeline_success(
    session_factory: sessionmaker[Session],
) -> None:
    _configure_plex(session_factory)
    runner = make_default_audio_pipeline_runner(
        session_factory, plex_runner=_StubPlexRunner(_PLEX_SUCCESS)
    )

    result = runner(FILE_PATH, _PREFERENCE)

    assert result.outcome is PipelineOutcome.SUCCESS
    assert result.success is True
    assert result.detail == _PLEX_SUCCESS.detail


def test_plex_nothing_to_do_outcome_maps_to_pipeline_nothing_to_do(
    session_factory: sessionmaker[Session],
) -> None:
    _configure_plex(session_factory)
    plex_result = PlexDefaultAudioResult(
        outcome=PlexDefaultAudioOutcome.NOTHING_TO_DO,
        success=True,
        detail="fewer than two streams to compare",
        rating_key="123",
    )
    runner = make_default_audio_pipeline_runner(
        session_factory, plex_runner=_StubPlexRunner(plex_result)
    )

    result = runner(FILE_PATH, _PREFERENCE)

    assert result.outcome is PipelineOutcome.NOTHING_TO_DO
    assert result.success is True
    assert result.detail == plex_result.detail


@pytest.mark.parametrize(
    "outcome",
    [
        PlexDefaultAudioOutcome.RATING_KEY_UNRESOLVED,
        PlexDefaultAudioOutcome.STREAM_FETCH_FAILED,
        PlexDefaultAudioOutcome.WRITE_FAILED,
        PlexDefaultAudioOutcome.VERIFY_FETCH_FAILED,
        PlexDefaultAudioOutcome.VERIFY_MISMATCH,
    ],
)
def test_every_plex_failure_outcome_maps_to_apply_failed(
    session_factory: sessionmaker[Session], outcome: PlexDefaultAudioOutcome
) -> None:
    _configure_plex(session_factory)
    plex_result = PlexDefaultAudioResult(
        outcome=outcome, success=False, detail=f"failed: {outcome.value}"
    )
    runner = make_default_audio_pipeline_runner(
        session_factory, plex_runner=_StubPlexRunner(plex_result)
    )

    result = runner(FILE_PATH, _PREFERENCE)

    assert result.outcome is PipelineOutcome.APPLY_FAILED
    assert result.success is False
    assert result.detail == plex_result.detail


# ---------------------------------------------------------------------------
# End-to-end: JobQueue(default_audio_pipeline_runner=make_default_audio_pipeline_runner(...))
# routes a real SET_DEFAULT_AUDIO job through exactly one mechanism.
# ---------------------------------------------------------------------------


def test_worker_pool_routes_a_set_default_audio_job_to_the_plex_mechanism_when_configured(
    session_factory: sessionmaker[Session],
) -> None:
    _configure_plex(session_factory)
    plex_runner = _StubPlexRunner(_PLEX_SUCCESS)
    remux_runner = _StubRemuxRunner(_REMUX_SUCCESS)
    queue: JobQueue = JobQueue(
        default_audio_pipeline_runner=make_default_audio_pipeline_runner(
            session_factory, plex_runner=plex_runner, remux_runner=remux_runner
        )
    )
    job = queue.enqueue_default_audio(FILE_PATH, _PREFERENCE)

    queue.start()
    queue.wait_idle()

    assert job.status is JobStatus.SUCCEEDED
    assert len(plex_runner.calls) == 1
    assert remux_runner.calls == []


def test_worker_pool_routes_a_set_default_audio_job_to_the_remux_mechanism_when_not_configured(
    session_factory: sessionmaker[Session],
) -> None:
    plex_runner = _StubPlexRunner(_PLEX_SUCCESS)
    remux_runner = _StubRemuxRunner(_REMUX_SUCCESS)
    queue: JobQueue = JobQueue(
        default_audio_pipeline_runner=make_default_audio_pipeline_runner(
            session_factory, plex_runner=plex_runner, remux_runner=remux_runner
        )
    )
    job = queue.enqueue_default_audio(FILE_PATH, _PREFERENCE)

    queue.start()
    queue.wait_idle()

    assert job.status is JobStatus.SUCCEEDED
    assert remux_runner.calls == [(FILE_PATH, _PREFERENCE, {"cancel_handle": job.cancellation})]
    assert plex_runner.calls == []


def test_worker_pool_never_invokes_both_mechanisms_for_the_same_job(
    session_factory: sessionmaker[Session],
) -> None:
    """COL-247's core acceptance criterion, exercised through a real JobQueue worker."""
    _configure_plex(session_factory)
    plex_runner = _StubPlexRunner(_PLEX_SUCCESS)
    remux_runner = _StubRemuxRunner(_REMUX_SUCCESS)
    queue: JobQueue = JobQueue(
        default_audio_pipeline_runner=make_default_audio_pipeline_runner(
            session_factory, plex_runner=plex_runner, remux_runner=remux_runner
        )
    )
    queue.enqueue_default_audio(FILE_PATH, _PREFERENCE)

    queue.start()
    queue.wait_idle()

    total_calls = len(plex_runner.calls) + len(remux_runner.calls)
    assert total_calls == 1


# ---------------------------------------------------------------------------
# JobQueue.from_settings wiring: the gate is the real production default.
# ---------------------------------------------------------------------------


def test_from_settings_defaults_default_audio_pipeline_runner_to_the_gate(
    settings: Settings,
) -> None:
    """Unlike the raw JobQueue() constructor, from_settings() wires the COL-247 gate by default."""
    from collapsarr.downmix.default_audio_pipeline import run_default_audio_pipeline

    def stub_pipeline_runner(*_args: object, **_kwargs: object) -> PipelineResult:
        return _REMUX_SUCCESS

    queue = JobQueue.from_settings(settings, pipeline_runner=stub_pipeline_runner)

    assert queue._default_audio_pipeline_runner is not run_default_audio_pipeline
    assert queue._default_audio_pipeline_runner.__module__ == (
        "collapsarr.jobs.default_audio_dispatch"
    )


def test_from_settings_still_accepts_an_explicit_default_audio_pipeline_runner_override(
    settings: Settings,
) -> None:
    """An explicit override bypasses the gate entirely, same as every other from_settings hook."""
    explicit: DefaultAudioPipelineRunner = _StubRemuxRunner(_REMUX_SUCCESS)

    queue = JobQueue.from_settings(
        settings,
        pipeline_runner=lambda *_a, **_k: _REMUX_SUCCESS,
        default_audio_pipeline_runner=explicit,
    )

    assert queue._default_audio_pipeline_runner is explicit


# ---------------------------------------------------------------------------
# Sanity-check the public re-export from collapsarr.jobs.
# ---------------------------------------------------------------------------


def test_default_audio_dispatch_reexports_from_package_root() -> None:
    from collapsarr.jobs import make_default_audio_pipeline_runner as reexported

    assert reexported is make_default_audio_pipeline_runner
