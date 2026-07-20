"""Tests for the downmix-failure -> notifier dispatch bridge (COL-37).

Every case drives :func:`~collapsarr.jobs.failure_notify.notify_job_failure`
(or :func:`~collapsarr.jobs.failure_notify.make_failure_notifier`, wired into
a real :class:`~collapsarr.jobs.queue.JobQueue`) with an
``httpx.MockTransport`` -- no live network call is made, matching the pattern
in ``test_notify_dispatch.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory, init_db
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.remux import RemuxResult
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.jobs.failure_notify import make_failure_notifier, notify_job_failure
from collapsarr.jobs.queue import Job, JobQueue, JobStatus, PipelineRunner
from collapsarr.notify.service import update_notifier_config

_SUCCESS = PipelineResult(outcome=PipelineOutcome.SUCCESS, success=True, detail="ok")
_REMUX_FAILURE = PipelineResult(
    outcome=PipelineOutcome.REMUX_FAILED,
    success=False,
    detail="ffmpeg remux failed for '/media/tv/The Show/S01E01.mkv' (exit code 1): boom",
    remux_result=RemuxResult(success=False, temp_file_path=None, returncode=1, stderr="boom"),
)


def _stub_runner(result: PipelineResult) -> PipelineRunner:
    def runner(file_path: Path, settings: DownmixSettings, **_: object) -> PipelineResult:
        return result

    return runner


def _ok_transport() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    return httpx.MockTransport(handler), seen


def _failed_job(
    *,
    file_path: str = "/media/tv/The Show/S01E01.mkv",
    settings: DownmixSettings | None = None,
    result: PipelineResult = _REMUX_FAILURE,
) -> Job:
    queue = JobQueue(pipeline_runner=_stub_runner(result))
    job = queue.enqueue(file_path, settings or DownmixSettings())
    queue.run_pending()
    assert job.status is JobStatus.FAILED
    return job


# ---------------------------------------------------------------------------
# notify_job_failure: payload shape (file, target/language, error).
# ---------------------------------------------------------------------------


def test_notify_job_failure_sends_file_target_language_and_error_to_enabled_webhook(
    session: Session,
) -> None:
    settings = DownmixSettings(
        enabled_targets=frozenset({DownmixTarget.STEREO, DownmixTarget.FIVE_POINT_ONE}),
        language_allow_list=frozenset({"eng", "jpn"}),
    )
    job = _failed_job(settings=settings)
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)
    transport, seen = _ok_transport()

    notify_job_failure(session, job, transport=transport)

    assert len(seen) == 1
    payload = json.loads(seen[0].content)
    assert payload["event_type"] == "downmix_failure"
    details = payload["details"]
    assert details["file"] == "/media/tv/The Show/S01E01.mkv"
    assert details["target"] == "5.1,stereo"
    assert details["language"] == "eng,jpn"
    assert details["error"] == _REMUX_FAILURE.detail


def test_notify_job_failure_omits_target_and_language_when_unset(session: Session) -> None:
    job = _failed_job(settings=DownmixSettings(enabled_targets=frozenset()))
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)
    transport, seen = _ok_transport()

    notify_job_failure(session, job, transport=transport)

    details = json.loads(seen[0].content)["details"]
    assert "target" not in details
    assert "language" not in details
    assert details["file"] == "/media/tv/The Show/S01E01.mkv"


def test_notify_job_failure_reports_the_unexpected_exception_as_the_error(
    session: Session,
) -> None:
    queue = JobQueue(
        pipeline_runner=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ffmpeg missing"))
    )
    job = queue.enqueue("/media/movie.mkv", DownmixSettings())
    queue.run_pending()
    assert job.status is JobStatus.FAILED

    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)
    transport, seen = _ok_transport()

    notify_job_failure(session, job, transport=transport)

    details = json.loads(seen[0].content)["details"]
    assert details["error"] == "ffmpeg missing"


def test_notify_job_failure_dispatches_to_discord_too(session: Session) -> None:
    job = _failed_job()
    update_notifier_config(
        session,
        discord_webhook_url="https://discord.com/api/webhooks/1/abc",
        discord_enabled=True,
    )
    transport, seen = _ok_transport()

    notify_job_failure(session, job, transport=transport)

    assert len(seen) == 1
    embed = json.loads(seen[0].content)["embeds"][0]
    assert embed["title"] == "Downmix failed"
    assert {"name": "error", "value": _REMUX_FAILURE.detail, "inline": True} in embed["fields"]


# ---------------------------------------------------------------------------
# No notifiers enabled: no network call, no error.
# ---------------------------------------------------------------------------


def test_notify_job_failure_makes_no_network_call_when_no_notifier_enabled(
    session: Session,
) -> None:
    job = _failed_job()
    transport, seen = _ok_transport()

    notify_job_failure(session, job, transport=transport)  # must not raise

    assert seen == []


# ---------------------------------------------------------------------------
# AC: notification failures (e.g. webhook unreachable) never raise.
# ---------------------------------------------------------------------------


def test_notify_job_failure_swallows_a_connection_error(session: Session) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    job = _failed_job()
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)

    notify_job_failure(session, job, transport=httpx.MockTransport(handler))  # must not raise


def test_notify_job_failure_swallows_an_http_error_status(session: Session) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    job = _failed_job()
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)

    notify_job_failure(session, job, transport=httpx.MockTransport(handler))  # must not raise


# ---------------------------------------------------------------------------
# End-to-end: JobQueue(failure_notifier=make_failure_notifier(...)) fires
# automatically from run_pending(), with no explicit notify_job_failure call.
# ---------------------------------------------------------------------------


def test_run_pending_automatically_dispatches_a_notification_when_wired_via_make_failure_notifier(
    settings: Settings,
) -> None:
    engine = create_engine_from_settings(settings)
    init_db(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as setup_session:
        update_notifier_config(
            setup_session, webhook_url="https://example.com/hook", webhook_enabled=True
        )

    transport, seen = _ok_transport()
    queue = JobQueue(
        pipeline_runner=_stub_runner(_REMUX_FAILURE),
        failure_notifier=make_failure_notifier(session_factory, transport=transport),
    )
    queue.enqueue("/media/tv/The Show/S01E01.mkv", DownmixSettings())

    queue.run_pending()  # note: no notify_job_failure(...) call anywhere here

    assert len(seen) == 1
    details = json.loads(seen[0].content)["details"]
    assert details["file"] == "/media/tv/The Show/S01E01.mkv"
    assert details["error"] == _REMUX_FAILURE.detail


def test_run_pending_does_not_dispatch_a_notification_for_a_succeeded_job(
    settings: Settings,
) -> None:
    engine = create_engine_from_settings(settings)
    init_db(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as setup_session:
        update_notifier_config(
            setup_session, webhook_url="https://example.com/hook", webhook_enabled=True
        )

    transport, seen = _ok_transport()
    queue = JobQueue(
        pipeline_runner=_stub_runner(_SUCCESS),
        failure_notifier=make_failure_notifier(session_factory, transport=transport),
    )
    queue.enqueue("/media/movie.mkv", DownmixSettings())

    queue.run_pending()

    assert seen == []


# ---------------------------------------------------------------------------
# Sanity-check the public re-exports from collapsarr.jobs.
# ---------------------------------------------------------------------------


def test_failure_notify_reexports_from_package_root() -> None:
    from collapsarr.jobs import make_failure_notifier as reexported_make
    from collapsarr.jobs import notify_job_failure as reexported_notify

    assert reexported_make is make_failure_notifier
    assert reexported_notify is notify_job_failure
