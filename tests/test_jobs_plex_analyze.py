"""Tests for the post-job Plex Analyze hook (COL-211).

Every case drives :func:`~collapsarr.jobs.plex_analyze.trigger_plex_analyze`
(or :func:`~collapsarr.jobs.plex_analyze.make_plex_analyzer`, wired into a
real :class:`~collapsarr.jobs.queue.JobQueue`) with an
``httpx.MockTransport`` -- no live network call is made, matching the
pattern in ``test_jobs_failure_notify.py``. The wiring contract itself
(never fails the job, never hangs the queue) is covered in
``test_jobs_queue.py`` alongside the other terminal hooks.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.remux import RemuxResult
from collapsarr.downmix.targets import DownmixSettings
from collapsarr.jobs.plex_analyze import make_plex_analyzer, trigger_plex_analyze
from collapsarr.jobs.queue import Job, JobQueue, JobStatus, PipelineRunner
from collapsarr.migrations import upgrade_to_head
from collapsarr.plex.models import PlexConnection, PlexLibraryItem
from collapsarr.plex.service import update_plex_connection

_SUCCESS = PipelineResult(outcome=PipelineOutcome.SUCCESS, success=True, detail="ok")
_REMUX_FAILURE = PipelineResult(
    outcome=PipelineOutcome.REMUX_FAILED,
    success=False,
    detail="ffmpeg remux failed",
    remux_result=RemuxResult(success=False, temp_file_path=None, returncode=1, stderr="boom"),
)

BASE_URL = "http://plex.local:32400"
TOKEN = "plex-token"
FILE_PATH = "/media/movie.mkv"
RATING_KEY = "12345"


def _stub_runner(result: PipelineResult) -> PipelineRunner:
    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        return result

    return runner


def _succeeded_job(*, file_path: str = FILE_PATH) -> Job:
    queue = JobQueue(pipeline_runner=_stub_runner(_SUCCESS))
    job = queue.enqueue(file_path, DownmixSettings())
    queue.start()
    queue.wait_idle()
    assert job.status is JobStatus.SUCCEEDED
    return job


def _failed_job(*, file_path: str = FILE_PATH) -> Job:
    queue = JobQueue(pipeline_runner=_stub_runner(_REMUX_FAILURE))
    job = queue.enqueue(file_path, DownmixSettings())
    queue.start()
    queue.wait_idle()
    assert job.status is JobStatus.FAILED
    return job


def _configure_plex(session: Session, *, transport: httpx.MockTransport) -> None:
    update_plex_connection(session, base_url=BASE_URL, token=TOKEN, transport=transport)


def _mapped(session: Session, *, file_path: str = FILE_PATH, rating_key: str = RATING_KEY) -> None:
    session.add(PlexLibraryItem(file_path=file_path, rating_key=rating_key, section_key="1"))
    session.commit()


def _tracking_transport(
    *, analyze_status: int = 200
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/analyze"):
            return httpx.Response(analyze_status)
        return httpx.Response(200, json={"MediaContainer": {"version": "1.0"}})

    return httpx.MockTransport(handler), seen


# ---------------------------------------------------------------------------
# trigger_plex_analyze: happy path -- resolves via the mapping table, calls Analyze.
# ---------------------------------------------------------------------------


def test_trigger_plex_analyze_calls_analyze_for_a_succeeded_job_with_a_mapped_rating_key(
    session: Session,
) -> None:
    transport, seen = _tracking_transport()
    _configure_plex(session, transport=transport)
    _mapped(session)
    job = _succeeded_job()

    trigger_plex_analyze(session, job, transport=transport)

    analyze_requests = [r for r in seen if r.url.path.endswith("/analyze")]
    assert len(analyze_requests) == 1
    assert analyze_requests[0].method == "PUT"
    assert analyze_requests[0].url.path == f"/library/metadata/{RATING_KEY}/analyze"
    assert analyze_requests[0].headers["X-Plex-Token"] == TOKEN


# ---------------------------------------------------------------------------
# No-op cases: never raises, no Analyze call.
# ---------------------------------------------------------------------------


def test_trigger_plex_analyze_is_a_noop_for_a_failed_job(session: Session) -> None:
    transport, seen = _tracking_transport()
    _configure_plex(session, transport=transport)
    _mapped(session)
    job = _failed_job()

    trigger_plex_analyze(session, job, transport=transport)

    assert [r for r in seen if r.url.path.endswith("/analyze")] == []


def test_trigger_plex_analyze_is_a_noop_when_plex_not_configured(session: Session) -> None:
    """No base_url ever saved -- the default, freshly-created connection row."""
    transport, seen = _tracking_transport()
    job = _succeeded_job()

    trigger_plex_analyze(session, job, transport=transport)  # must not raise

    assert seen == []


def test_trigger_plex_analyze_is_a_noop_when_the_rating_key_cannot_be_resolved(
    session: Session,
) -> None:
    """Plex configured, but the file is neither mapped nor tracked (no live-fallback scope)."""
    transport, seen = _tracking_transport()
    _configure_plex(session, transport=transport)
    job = _succeeded_job()

    trigger_plex_analyze(session, job, transport=transport)  # must not raise

    assert [r for r in seen if r.url.path.endswith("/analyze")] == []


def test_trigger_plex_analyze_swallows_an_analyze_error_response(session: Session) -> None:
    transport, seen = _tracking_transport(analyze_status=500)
    _configure_plex(session, transport=transport)
    _mapped(session)
    job = _succeeded_job()

    trigger_plex_analyze(session, job, transport=transport)  # must not raise

    assert len([r for r in seen if r.url.path.endswith("/analyze")]) == 1


def test_trigger_plex_analyze_swallows_a_connection_error(session: Session) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, json={"MediaContainer": {"version": "1.0"}})
        raise httpx.ConnectError("Connection refused", request=request)

    ok_transport = httpx.MockTransport(handler)
    _configure_plex(session, transport=ok_transport)
    _mapped(session)
    job = _succeeded_job()

    trigger_plex_analyze(session, job, transport=ok_transport)  # must not raise


def test_trigger_plex_analyze_swallows_an_unexpected_exception(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-braces: even a failure reading the Plex connection itself never raises."""
    import collapsarr.jobs.plex_analyze as plex_analyze_module

    def _boom(_session: Session) -> PlexConnection:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(plex_analyze_module, "get_plex_connection", _boom)
    job = _succeeded_job()

    trigger_plex_analyze(session, job)  # must not raise


# ---------------------------------------------------------------------------
# End-to-end: JobQueue(plex_analyzer=make_plex_analyzer(...)) fires
# automatically from the worker pool, with no explicit trigger_plex_analyze call.
# ---------------------------------------------------------------------------


def test_worker_pool_automatically_triggers_analyze_when_wired_via_make_plex_analyzer(
    settings: Settings,
) -> None:
    engine = create_engine_from_settings(settings)
    upgrade_to_head(settings)
    session_factory = create_session_factory(engine)

    transport, seen = _tracking_transport()
    with session_factory() as setup_session:
        _configure_plex(setup_session, transport=transport)
        _mapped(setup_session)

    queue = JobQueue(
        pipeline_runner=_stub_runner(_SUCCESS),
        plex_analyzer=make_plex_analyzer(session_factory, transport=transport),
    )
    queue.enqueue(FILE_PATH, DownmixSettings())

    queue.start()
    queue.wait_idle()  # note: no trigger_plex_analyze(...) call anywhere here

    analyze_requests = [r for r in seen if r.url.path.endswith("/analyze")]
    assert len(analyze_requests) == 1


def test_worker_pool_does_not_trigger_analyze_for_a_failed_job(settings: Settings) -> None:
    engine = create_engine_from_settings(settings)
    upgrade_to_head(settings)
    session_factory = create_session_factory(engine)

    transport, seen = _tracking_transport()
    with session_factory() as setup_session:
        _configure_plex(setup_session, transport=transport)
        _mapped(setup_session)

    queue = JobQueue(
        pipeline_runner=_stub_runner(_REMUX_FAILURE),
        plex_analyzer=make_plex_analyzer(session_factory, transport=transport),
    )
    queue.enqueue(FILE_PATH, DownmixSettings())

    queue.start()
    queue.wait_idle()

    assert [r for r in seen if r.url.path.endswith("/analyze")] == []


# ---------------------------------------------------------------------------
# Sanity-check the public re-exports from collapsarr.jobs.
# ---------------------------------------------------------------------------


def test_plex_analyze_reexports_from_package_root() -> None:
    from collapsarr.jobs import make_plex_analyzer as reexported_make
    from collapsarr.jobs import trigger_plex_analyze as reexported_trigger

    assert reexported_make is make_plex_analyzer
    assert reexported_trigger is trigger_plex_analyze
