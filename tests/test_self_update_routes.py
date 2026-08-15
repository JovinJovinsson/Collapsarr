"""Contract tests for the Self-Update status + apply endpoints (COL-230, COL-232).

Covers ``GET /api/system/self-update/status``: the API-gate behaviour (401
without a key/session, mirroring ``tests/test_update_check_routes.py``), the
response shape, and that it get-or-creates the singleton row rather than
404ing before any self-update flow has ever run. Also covers ``POST
/api/system/self-update/apply`` (COL-232): the install-method gate (403), the
"no newer stable release" gate (409), the in-progress guard (409), a full
success round-trip with a mocked transport + fake subprocess/re-exec doubles
(no real network call, subprocess spawn, or ``os.execv``), and the
checksum-mismatch failure shape (502).
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
import threading
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.downmix.cancellation import CancellationHandle
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixSettings
from collapsarr.jobs.queue import JobQueue, JobStatus, PipelineRunner
from collapsarr.jobs.scheduler import JobScheduler
from collapsarr.main import create_app
from collapsarr.self_update.apply import PIPX_UPGRADE_COMMAND
from collapsarr.self_update.models import PHASE_AWAITING_HEALTH, PHASE_IDLE, PHASE_VERIFYING
from collapsarr.self_update.service import (
    begin_self_update,
    get_self_update_state,
    set_self_update_phase,
)
from collapsarr.settings.models import UPDATE_CHANNEL_STABLE
from collapsarr.settings.service import get_global_settings
from collapsarr.update_check.models import UPDATE_CHECK_STATE_ID, UpdateCheckState


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #


def test_get_status_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/system/self-update/status").status_code == 401


# --------------------------------------------------------------------------- #
# Response shape
# --------------------------------------------------------------------------- #


def test_get_status_returns_idle_before_any_self_update_has_run(client: TestClient) -> None:
    response = client.get("/api/system/self-update/status", headers=_auth_headers(client))

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "in_progress": False,
        "phase": "idle",
        "previous_version": None,
    }


def test_get_status_reflects_an_in_progress_self_update(client: TestClient) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        assert isinstance(session, Session)
        begin_self_update(session, previous_version="1.0.0")
        set_self_update_phase(session, PHASE_VERIFYING)

    response = client.get("/api/system/self-update/status", headers=_auth_headers(client))

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "in_progress": True,
        "phase": "verifying",
        "previous_version": "1.0.0",
    }


# --------------------------------------------------------------------------- #
# POST /api/system/self-update/apply (COL-232)
# --------------------------------------------------------------------------- #

_TAG = "v9.9.9"
_VERSION = "9.9.9"
_FILENAME = f"collapsarr-{_VERSION}-linux-amd64.tar.gz"
_ARCHIVE_BYTES = b"pretend this is a real release archive's bytes"
_ARCHIVE_SHA256 = hashlib.sha256(_ARCHIVE_BYTES).hexdigest()
_SHA256SUMS_CONTENT = f"{_ARCHIVE_SHA256}  {_FILENAME}\n"


class _RunnerSpy:
    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[Sequence[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(
        self, command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        return subprocess.CompletedProcess(
            list(command), self.returncode, stdout="", stderr=self.stderr
        )


class _ReexecSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _offline_update_check_transport() -> httpx.MockTransport:
    """Deterministic, offline stand-in for the startup Update Check tick.

    Mirrors ``conftest.py``'s private helper of the same shape -- redefined
    locally to keep this file self-contained, matching
    ``tests/test_ffmpeg_download_routes.py``'s own convention.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="offline in tests")

    return httpx.MockTransport(handler)


def _self_update_transport(
    *, sha256sums: str = _SHA256SUMS_CONTENT, archive: bytes = _ARCHIVE_BYTES
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/SHA256SUMS"):
            return httpx.Response(200, text=sha256sums)
        if path.endswith(f"/{_FILENAME}"):
            return httpx.Response(200, content=archive)
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


class _SpawnSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, args: object, staged_dir: object) -> None:
        self.calls += 1


class _ExitSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _app(
    settings: Settings,
    *,
    self_update_transport: httpx.MockTransport | None = None,
    self_update_subprocess_runner: _RunnerSpy | None = None,
    self_update_reexec_fn: _ReexecSpy | None = None,
    self_update_handoff_spawner: _SpawnSpy | None = None,
    self_update_exit_fn: _ExitSpy | None = None,
) -> FastAPI:
    return create_app(
        settings=settings,
        update_check_transport=_offline_update_check_transport(),
        self_update_transport=self_update_transport,
        self_update_subprocess_runner=self_update_subprocess_runner,
        self_update_reexec_fn=self_update_reexec_fn,
        self_update_handoff_spawner=self_update_handoff_spawner,
        self_update_exit_fn=self_update_exit_fn,
    )


def _seed_stable_update_available(client: TestClient, *, latest_tag: str = _TAG) -> None:
    """Persist an ``UpdateCheckState`` row reporting a newer stable release.

    Writes the row directly (rather than driving a real Update Check tick)
    -- the apply endpoint only ever reads this persisted state
    (:func:`~collapsarr.self_update.apply.stable_update_target`), so this is
    the most direct way to put it in the "an update is available" state the
    apply endpoint's own gate requires.
    """
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        assert isinstance(session, Session)
        session.merge(
            UpdateCheckState(
                id=UPDATE_CHECK_STATE_ID, channel=UPDATE_CHANNEL_STABLE, latest_tag=latest_tag
            )
        )
        session.commit()


def _apply(client: TestClient) -> httpx.Response:
    return client.post(  # type: ignore[no-any-return]
        "/api/system/self-update/apply", headers=_auth_headers(client)
    )


def test_apply_requires_authentication(client: TestClient) -> None:
    assert client.post("/api/system/self-update/apply").status_code == 401


def test_apply_is_unavailable_for_non_pipx_installs(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.self_update.routes.install_method", lambda: "docker")
    with TestClient(_app(settings)) as client:
        _seed_stable_update_available(client)
        response = _apply(client)

    assert response.status_code == 403
    assert "pipx" in response.json()["detail"].lower()


def test_apply_rejects_when_no_stable_update_is_available(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.self_update.routes.install_method", lambda: "pipx")
    with TestClient(_app(settings)) as client:
        # No UpdateCheckState row seeded at all -- nothing available yet.
        response = _apply(client)

    assert response.status_code == 409
    assert "no newer" in response.json()["detail"].lower()


def test_apply_rejects_a_concurrent_trigger(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.self_update.routes.install_method", lambda: "pipx")
    with TestClient(_app(settings)) as client:
        _seed_stable_update_available(client)
        with app_session(client) as session:
            begin_self_update(session, previous_version="1.0.0")

        response = _apply(client)

    assert response.status_code == 409


def app_session(client: TestClient) -> Session:
    app = client.app
    assert isinstance(app, FastAPI)
    session: Session = app.state.session_factory()
    return session


def test_apply_succeeds_downloads_verifies_upgrades_and_reexecs(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.self_update.routes.install_method", lambda: "pipx")
    monkeypatch.setattr(
        "collapsarr.self_update.apply.resolve_platform_arch", lambda: ("linux", "amd64")
    )
    runner = _RunnerSpy(returncode=0)
    reexec = _ReexecSpy()

    with TestClient(
        _app(
            settings,
            self_update_transport=_self_update_transport(),
            self_update_subprocess_runner=runner,
            self_update_reexec_fn=reexec,
        )
    ) as client:
        _seed_stable_update_available(client)
        response = _apply(client)

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert len(runner.calls) == 1
        assert runner.calls[0] == PIPX_UPGRADE_COMMAND
        assert reexec.calls == 1

        with app_session(client) as session:
            state = get_self_update_state(session)
            # Guard handed off across the re-exec (COL-232), not cleared.
            assert state.in_progress is True
            assert state.phase == PHASE_AWAITING_HEALTH


def _native_release_tar() -> bytes:
    """A real ``.tar.gz`` wrapping the install tree in a top-level ``collapsarr/`` dir."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = b"#!/new-binary\n"
        info = tarfile.TarInfo(name="collapsarr/collapsarr")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_apply_native_install_stages_spawns_handoff_and_exits(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("collapsarr.self_update.routes.install_method", lambda: "native")
    monkeypatch.setattr(
        "collapsarr.self_update.native.resolve_platform_arch", lambda: ("linux", "amd64")
    )
    # Keep the swap machinery pointed at a throwaway install dir, never the real
    # test-runner executable's directory.
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "collapsarr").write_bytes(b"OLD")
    monkeypatch.setattr(
        "collapsarr.self_update.native.resolve_install_dir", lambda: install_dir
    )
    archive = _native_release_tar()
    sums = f"{hashlib.sha256(archive).hexdigest()}  {_FILENAME}\n"
    spawn = _SpawnSpy()
    exit_fn = _ExitSpy()

    with TestClient(
        _app(
            settings,
            self_update_transport=_self_update_transport(sha256sums=sums, archive=archive),
            self_update_handoff_spawner=spawn,
            self_update_exit_fn=exit_fn,
        )
    ) as client:
        _seed_stable_update_available(client)
        response = _apply(client)

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert spawn.calls == 1
        assert exit_fn.calls == 1

        with app_session(client) as session:
            state = get_self_update_state(session)
            # Guard handed off across the process boundary (COL-235), not cleared.
            assert state.in_progress is True
            assert state.phase == PHASE_AWAITING_HEALTH


def test_apply_checksum_mismatch_reports_a_clear_error_and_never_upgrades(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.self_update.routes.install_method", lambda: "pipx")
    monkeypatch.setattr(
        "collapsarr.self_update.apply.resolve_platform_arch", lambda: ("linux", "amd64")
    )
    runner = _RunnerSpy()
    reexec = _ReexecSpy()
    wrong_sha256sums = f"{'0' * 64}  {_FILENAME}\n"

    with TestClient(
        _app(
            settings,
            self_update_transport=_self_update_transport(sha256sums=wrong_sha256sums),
            self_update_subprocess_runner=runner,
            self_update_reexec_fn=reexec,
        )
    ) as client:
        _seed_stable_update_available(client)
        response = _apply(client)

        assert response.status_code == 502
        detail = response.json()["detail"].lower()
        assert "sha-256" in detail or "mismatch" in detail
        assert runner.calls == []
        assert reexec.calls == 0

        with app_session(client) as session:
            state = get_self_update_state(session)
            assert state.in_progress is False
            assert state.phase == PHASE_IDLE


# --------------------------------------------------------------------------- #
# COL-233: in-flight Job handling (Cancel & Restart Now / Wait & Restart)
# --------------------------------------------------------------------------- #

_SURROUND: list[AudioStreamInfo] = [
    AudioStreamInfo(index=0, codec="ac3", channels=6, channel_layout="5.1(side)", language="eng")
]
_SUCCESS_RESULT = PipelineResult(outcome=PipelineOutcome.SUCCESS, success=True, detail="ok")
_FAILED_RESULT = PipelineResult(
    outcome=PipelineOutcome.REMUX_FAILED, success=False, detail="killed"
)


def _surround_probe(_path: Path) -> Sequence[AudioStreamInfo]:
    return _SURROUND


class _FakeProcess:
    """A stand-in for a job's live subprocess, killable by the cancel handle."""

    def __init__(self) -> None:
        self.pid = 2_000_000_000  # no such process -> os.getpgid raises, forcing kill()
        self.killed = threading.Event()

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:
        self.killed.set()


class _HardKillOnceRunner:
    """A pipeline_runner whose ``victim`` job blocks until killed -- once.

    Mirrors ``tests/test_self_update_apply.py``'s runner of the same name --
    only the *first* call for ``victim`` blocks/attaches a cancel handle; a
    second call (the requeued Job "Cancel & Restart Now" creates) runs
    straight to success.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.process = _FakeProcess()
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(
        self,
        file_path: Path,
        _settings: DownmixSettings,
        *,
        cancel_handle: CancellationHandle | None = None,
        **_: object,
    ) -> PipelineResult:
        with self._lock:
            self.calls += 1
            first_call = self.calls == 1
        if file_path.stem == "victim" and first_call:
            assert cancel_handle is not None, "queue must thread a cancel handle into a RUNNING job"
            cancel_handle.attach(self.process)
            self.started.set()
            assert self.process.killed.wait(timeout=5), "victim subprocess was never killed"
            cancel_handle.detach(self.process)
            return _FAILED_RESULT
        return _SUCCESS_RESULT


class _GatedRunner:
    """A pipeline_runner whose ``victim`` job blocks until released, then succeeds."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, file_path: Path, _settings: DownmixSettings, **_: object) -> PipelineResult:
        if file_path.stem == "victim":
            self.started.set()
            assert self.release.wait(timeout=5), "victim job was never released"
        return _SUCCESS_RESULT


def _attach_real_queue_and_scheduler(
    client: TestClient, settings: Settings, *, pipeline_runner: PipelineRunner
) -> tuple[JobQueue, JobScheduler]:
    """Wire a real, started :class:`JobQueue` + :class:`JobScheduler` onto ``client.app.state``.

    Mirrors ``tests/test_jobs_routes.py``'s ``test_cancel_job_hard_kills_a_
    running_job_end_to_end`` -- the ``client`` fixture doesn't set
    ``enable_scheduler=True`` (no real ffmpeg pipeline is available in CI),
    so a real queue/scheduler pair (with an injected fake pipeline_runner)
    is attached directly instead, driving this endpoint's in-flight-Job
    handling genuinely end to end over HTTP.
    """
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    queue = JobQueue(max_concurrency=1, pipeline_runner=pipeline_runner)
    queue.start()
    scheduler = JobScheduler(queue, session_factory, settings, probe=_surround_probe)
    app.state.job_queue = queue
    app.state.job_scheduler = scheduler
    return queue, scheduler


def test_apply_rejects_an_unknown_flow_value(client: TestClient) -> None:
    response = client.post(
        "/api/system/self-update/apply",
        json={"flow": "not_a_real_flow"},
        headers=_auth_headers(client),
    )

    assert response.status_code == 422


def test_apply_returns_503_for_a_flow_when_no_job_queue_is_wired(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.self_update.routes.install_method", lambda: "pipx")
    with TestClient(_app(settings)) as client:
        _seed_stable_update_available(client)

        response = client.post(
            "/api/system/self-update/apply",
            json={"flow": "wait_and_restart"},
            headers=_auth_headers(client),
        )

    assert response.status_code == 503


def test_apply_rejects_a_flow_choice_for_native_installs(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``flow`` on a ``native`` install is a clear 409, not a crash or silent no-op.

    Merge-time reconciliation (COL-233 x COL-235): ``apply_with_flow`` only
    wraps the ``pipx`` apply path -- extending it to the native staged-handoff
    path is explicitly out of scope here (see ``routes.py``'s module and
    function docstrings). Checked *before* the job-queue-wiring/running-count
    gates, so this 409 fires even with no queue wired at all -- mirrors
    ``test_apply_returns_503_for_a_flow_when_no_job_queue_is_wired``'s shape
    for the pipx case, but for native the outcome is a 409 naming the
    unsupported combination, never a 503.
    """
    monkeypatch.setattr("collapsarr.self_update.routes.install_method", lambda: "native")
    with TestClient(_app(settings)) as client:
        _seed_stable_update_available(client)

        response = client.post(
            "/api/system/self-update/apply",
            json={"flow": "wait_and_restart"},
            headers=_auth_headers(client),
        )

    assert response.status_code == 409
    detail = response.json()["detail"].lower()
    assert "flow" in detail
    assert "native" in detail


def test_apply_rejects_when_jobs_running_and_no_flow_chosen(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.self_update.routes.install_method", lambda: "pipx")
    with TestClient(_app(settings)) as client:
        _seed_stable_update_available(client)
        runner = _HardKillOnceRunner()
        queue, scheduler = _attach_real_queue_and_scheduler(
            client, settings, pipeline_runner=runner
        )
        try:
            victim = scheduler.trigger_file("/media/victim.mkv")
            assert victim is not None
            assert runner.started.wait(timeout=5)

            response = _apply(client)

            assert response.status_code == 409
            assert "flow" in response.json()["detail"].lower()
        finally:
            runner.process.kill()  # unblock the worker so shutdown() doesn't hang
            queue.shutdown()


def test_apply_cancel_and_restart_flow_over_http_end_to_end(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.self_update.routes.install_method", lambda: "pipx")
    monkeypatch.setattr(
        "collapsarr.self_update.apply.resolve_platform_arch", lambda: ("linux", "amd64")
    )
    runner = _HardKillOnceRunner()
    subprocess_runner = _RunnerSpy(returncode=0)
    reexec = _ReexecSpy()

    with TestClient(
        _app(
            settings,
            self_update_transport=_self_update_transport(),
            self_update_subprocess_runner=subprocess_runner,
            self_update_reexec_fn=reexec,
        )
    ) as client:
        _seed_stable_update_available(client)
        queue, scheduler = _attach_real_queue_and_scheduler(
            client, settings, pipeline_runner=runner
        )
        try:
            victim = scheduler.trigger_file("/media/victim.mkv")
            assert victim is not None
            assert runner.started.wait(timeout=5)

            response = client.post(
                "/api/system/self-update/apply",
                json={"flow": "cancel_and_restart"},
                headers=_auth_headers(client),
            )

            assert response.status_code == 200, response.text
            assert response.json() == {"ok": True}
            assert runner.process.killed.is_set()
            assert victim.status is JobStatus.FAILED
            assert len(subprocess_runner.calls) == 1
            assert reexec.calls == 1

            # A second, distinct Job for the same file now exists -- the
            # hard-cancelled Job was immediately requeued.
            requeued = [
                job
                for job in queue.list_jobs()
                if job.file_path == Path("/media/victim.mkv") and job.id != victim.id
            ]
            assert len(requeued) == 1

            with app_session(client) as session:
                row = get_global_settings(session)
                # Forced for the duration; left forced on success -- the
                # next boot (collapsarr.main's lifespan) restores it.
                assert row.auto_processing_paused is True
                assert row.auto_processing_pause_restore_value is False
        finally:
            queue.shutdown()


def test_apply_wait_and_restart_flow_over_http_end_to_end(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.self_update.routes.install_method", lambda: "pipx")
    monkeypatch.setattr(
        "collapsarr.self_update.apply.resolve_platform_arch", lambda: ("linux", "amd64")
    )
    runner = _GatedRunner()
    subprocess_runner = _RunnerSpy(returncode=0)
    reexec = _ReexecSpy()

    with TestClient(
        _app(
            settings,
            self_update_transport=_self_update_transport(),
            self_update_subprocess_runner=subprocess_runner,
            self_update_reexec_fn=reexec,
        )
    ) as client:
        _seed_stable_update_available(client)
        queue, scheduler = _attach_real_queue_and_scheduler(
            client, settings, pipeline_runner=runner
        )
        try:
            victim = scheduler.trigger_file("/media/victim.mkv")
            assert victim is not None
            assert runner.started.wait(timeout=5)

            # Release the victim Job shortly after the request starts
            # blocking in the wait step -- long enough that the request is
            # genuinely mid-wait, short enough to keep the test fast.
            threading.Timer(0.2, runner.release.set).start()

            response = client.post(
                "/api/system/self-update/apply",
                json={"flow": "wait_and_restart"},
                headers=_auth_headers(client),
            )

            assert response.status_code == 200, response.text
            assert response.json() == {"ok": True}
            assert victim.status is JobStatus.SUCCEEDED  # finished naturally, never FAILED
            # Nothing was cancelled or requeued for this file.
            assert [j for j in queue.list_jobs() if j.file_path == Path("/media/victim.mkv")] == [
                victim
            ]

            with app_session(client) as session:
                row = get_global_settings(session)
                assert row.auto_processing_paused is True
                assert row.auto_processing_pause_restore_value is False
        finally:
            queue.shutdown()
