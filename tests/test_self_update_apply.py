"""Tests for the pipx self-update apply flow (COL-232).

Exercised through the bare service-layer ``session`` fixture (no HTTP app),
mirroring ``tests/test_self_update_service.py``'s convention -- this module
has no FastAPI dependency of its own. ``resolve_platform_arch`` is
monkeypatched to a fixed platform/arch so the fixture data (SHA256SUMS
content, expected filename) stays deterministic across the CI matrix's
actual host platform. Both OS-touching seams (``subprocess_runner``,
``reexec_fn``) are fakes/spies throughout -- per the AC, no real subprocess
spawn or re-exec ever happens in this file.
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from collapsarr import __version__
from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.downmix.cancellation import CancellationHandle
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixSettings
from collapsarr.jobs.history import make_history_recorder
from collapsarr.jobs.queue import Job, JobQueue, JobStatus, PipelineRunner
from collapsarr.jobs.scheduler import JobScheduler
from collapsarr.migrations import upgrade_to_head
from collapsarr.self_update.apply import (
    CANCEL_DRAIN_TIMEOUT,
    FLOW_CANCEL_AND_RESTART,
    FLOW_WAIT_AND_RESTART,
    PIPX_UPGRADE_COMMAND,
    WAIT_AND_RESTART_TIMEOUT,
    SelfUpdateApplyOutcome,
    apply_pipx_update,
    apply_with_flow,
    stable_update_target,
)
from collapsarr.self_update.models import PHASE_AWAITING_HEALTH, PHASE_IDLE, SelfUpdateState
from collapsarr.self_update.service import (
    SelfUpdateAlreadyInProgressError,
    begin_self_update,
    get_self_update_state,
)
from collapsarr.settings.models import UPDATE_CHANNEL_BETA, UPDATE_CHANNEL_STABLE
from collapsarr.settings.service import get_global_settings, update_global_settings
from collapsarr.update_check.client import GITHUB_REPO
from collapsarr.update_check.models import UpdateCheckState

_TAG = "v1.2.3"
_VERSION = "1.2.3"
_FILENAME = f"collapsarr-{_VERSION}-linux-amd64.tar.gz"
_ARCHIVE_BYTES = b"pretend this is a real release archive's bytes"
_ARCHIVE_SHA256 = hashlib.sha256(_ARCHIVE_BYTES).hexdigest()
_SHA256SUMS_CONTENT = f"{_ARCHIVE_SHA256}  {_FILENAME}\n"


class _RunnerSpy:
    """A fake :data:`~collapsarr.self_update.apply.SubprocessRunner`.

    Records every call and returns a fixed :class:`subprocess.CompletedProcess`
    -- never spawns a real ``pipx`` process.
    """

    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[Sequence[str], float]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(
        self, command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, timeout))
        return subprocess.CompletedProcess(
            list(command), self.returncode, stdout="", stderr=self.stderr
        )


class _ReexecSpy:
    """A fake :data:`~collapsarr.self_update.apply.ReexecFn`.

    Records that it was called and returns normally -- a real ``os.execv``
    never returns.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _transport(
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


def _patch_platform(monkeypatch: pytest.MonkeyPatch, resolved: tuple[str, str] | None) -> None:
    monkeypatch.setattr(
        "collapsarr.self_update.apply.resolve_platform_arch", lambda: resolved
    )


# --------------------------------------------------------------------------- #
# stable_update_target
# --------------------------------------------------------------------------- #


def test_stable_update_target_is_none_before_any_tick_has_run() -> None:
    assert stable_update_target(None, "1.0.0") is None


def test_stable_update_target_is_none_when_no_tag_resolved_yet() -> None:
    state = UpdateCheckState(channel=UPDATE_CHANNEL_STABLE, latest_tag=None)
    assert stable_update_target(state, "1.0.0") is None


def test_stable_update_target_is_none_for_a_beta_channel_row() -> None:
    state = UpdateCheckState(channel=UPDATE_CHANNEL_BETA, latest_tag="beta-v1.2.3.0007")
    assert stable_update_target(state, "1.0.0") is None


def test_stable_update_target_is_none_when_already_up_to_date() -> None:
    state = UpdateCheckState(channel=UPDATE_CHANNEL_STABLE, latest_tag="v1.2.3")
    assert stable_update_target(state, "1.2.3") is None


def test_stable_update_target_returns_the_latest_tag_when_newer() -> None:
    state = UpdateCheckState(channel=UPDATE_CHANNEL_STABLE, latest_tag="v1.2.3")
    assert stable_update_target(state, "1.0.0") == "v1.2.3"


# --------------------------------------------------------------------------- #
# AC: happy path -- download, verify, pipx upgrade, re-exec
# --------------------------------------------------------------------------- #


def test_apply_pipx_update_happy_path(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _RunnerSpy(returncode=0)
    reexec = _ReexecSpy()

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        subprocess_runner=runner,
        reexec_fn=reexec,
    )

    assert isinstance(outcome, SelfUpdateApplyOutcome)
    assert outcome.ok is True
    assert outcome.error is None

    assert len(runner.calls) == 1
    assert runner.calls[0][0] == PIPX_UPGRADE_COMMAND
    assert reexec.calls == 1

    state = get_self_update_state(session)
    assert state.in_progress is True  # guard handed off across the re-exec, not cleared
    assert state.phase == PHASE_AWAITING_HEALTH
    assert state.previous_version == __version__


def test_apply_pipx_update_requests_the_expected_release_asset_urls(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/SHA256SUMS"):
            return httpx.Response(200, text=_SHA256SUMS_CONTENT)
        return httpx.Response(200, content=_ARCHIVE_BYTES)

    apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=httpx.MockTransport(handler),
        subprocess_runner=_RunnerSpy(),
        reexec_fn=_ReexecSpy(),
    )

    assert f"https://github.com/{GITHUB_REPO}/releases/download/{_TAG}/SHA256SUMS" in seen
    assert (
        f"https://github.com/{GITHUB_REPO}/releases/download/{_TAG}/{_FILENAME}" in seen
    )


# --------------------------------------------------------------------------- #
# AC: checksum mismatch aborts cleanly, never touching pipx upgrade
# --------------------------------------------------------------------------- #


def test_apply_pipx_update_checksum_mismatch_aborts_without_upgrading(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    wrong_sha256sums = f"{'0' * 64}  {_FILENAME}\n"
    runner = _RunnerSpy()
    reexec = _ReexecSpy()

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=_transport(sha256sums=wrong_sha256sums),
        subprocess_runner=runner,
        reexec_fn=reexec,
    )

    assert outcome.ok is False
    assert outcome.error is not None
    assert "sha-256" in outcome.error.lower() or "mismatch" in outcome.error.lower()

    # pipx upgrade must never have been reached, and no re-exec attempted.
    assert runner.calls == []
    assert reexec.calls == 0

    # The running install is untouched -- the guard is cleared back to idle.
    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


def test_apply_pipx_update_missing_checksum_entry_aborts_without_upgrading(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _RunnerSpy()
    reexec = _ReexecSpy()

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        # No SHA256SUMS entry for this release at all.
        transport=_transport(sha256sums=""),
        subprocess_runner=runner,
        reexec_fn=reexec,
    )

    assert outcome.ok is False
    assert runner.calls == []
    assert reexec.calls == 0

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


# --------------------------------------------------------------------------- #
# AC: a second, concurrent trigger is rejected
# --------------------------------------------------------------------------- #


def test_apply_pipx_update_rejects_a_concurrent_trigger(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    begin_self_update(session, previous_version="0.9.0")
    runner = _RunnerSpy()
    reexec = _ReexecSpy()

    with pytest.raises(SelfUpdateAlreadyInProgressError):
        apply_pipx_update(
            session,
            target_tag=_TAG,
            transport=_transport(),
            subprocess_runner=runner,
            reexec_fn=reexec,
        )

    # No network/subprocess/re-exec activity from the rejected attempt.
    assert runner.calls == []
    assert reexec.calls == 0

    state = get_self_update_state(session)
    assert state.previous_version == "0.9.0"  # the first attempt's, untouched


# --------------------------------------------------------------------------- #
# pipx upgrade itself fails (non-zero exit)
# --------------------------------------------------------------------------- #


def test_apply_pipx_update_reports_a_failed_pipx_upgrade_without_reexecing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _RunnerSpy(returncode=1, stderr="pipx: package not found")
    reexec = _ReexecSpy()

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        subprocess_runner=runner,
        reexec_fn=reexec,
    )

    assert outcome.ok is False
    assert outcome.error is not None
    assert "exit 1" in outcome.error
    assert "pipx: package not found" in outcome.error
    assert reexec.calls == 0

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


# --------------------------------------------------------------------------- #
# Code-review must-fix: any unexpected exception inside the guarded region
# clears the guard too, not just the specific failure modes handled above --
# a poisoned in_progress=True row would otherwise block every future
# self-update attempt (begin_self_update refuses whenever it's already set),
# and it's a persisted row that would survive a process restart.
# --------------------------------------------------------------------------- #


class _RaisingRunner:
    """A fake :data:`~collapsarr.self_update.apply.SubprocessRunner` that raises.

    ``PermissionError`` (a real-world case: a ``pipx`` on ``PATH`` that
    exists but isn't executable) is deliberately *not* one of the two
    exception types :func:`~collapsarr.self_update.apply.apply_pipx_update`
    special-cases (``FileNotFoundError``/``subprocess.TimeoutExpired``), so
    this exercises the broad catch-all rather than either specific branch.
    """

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def __call__(
        self, command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        raise self.exc


def test_apply_pipx_update_clears_the_guard_on_an_unexpected_subprocess_exception(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _RaisingRunner(PermissionError("[Errno 13] Permission denied: 'pipx'"))
    reexec = _ReexecSpy()

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        subprocess_runner=runner,
        reexec_fn=reexec,
    )

    assert outcome.ok is False
    assert outcome.error is not None
    assert "permission" in outcome.error.lower()
    assert runner.calls == 1
    assert reexec.calls == 0

    # The must-fix: the guard is cleared, not left poisoned -- a subsequent
    # attempt must be able to start.
    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE

    # Proof the guard genuinely recovered: a fresh attempt can begin.
    begin_self_update(session, previous_version="1.0.0")


def test_apply_pipx_update_clears_the_guard_on_an_unexpected_reexec_exception(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _RunnerSpy(returncode=0)

    def _exploding_reexec() -> None:
        raise OSError("exec format error")

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        subprocess_runner=runner,
        reexec_fn=_exploding_reexec,
    )

    assert outcome.ok is False
    assert outcome.error is not None
    assert len(runner.calls) == 1  # pipx upgrade did run before the re-exec blew up

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


# --------------------------------------------------------------------------- #
# Unsupported platform
# --------------------------------------------------------------------------- #


def test_apply_pipx_update_unsupported_platform_fails_before_taking_the_guard(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, None)
    runner = _RunnerSpy()
    reexec = _ReexecSpy()

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        subprocess_runner=runner,
        reexec_fn=reexec,
    )

    assert outcome.ok is False
    assert runner.calls == []
    assert reexec.calls == 0

    state = get_self_update_state(session)
    assert isinstance(state, SelfUpdateState)
    assert state.in_progress is False  # the guard was never even reserved


# --------------------------------------------------------------------------- #
# COL-233: apply_with_flow -- in-flight Job handling (Cancel & Restart Now /
# Wait & Restart), plus the Auto-Processing Pause stash/force-pause/restore
# bookkeeping both flows trigger around it.
# --------------------------------------------------------------------------- #

_SURROUND: list[AudioStreamInfo] = [
    AudioStreamInfo(index=0, codec="ac3", channels=6, channel_layout="5.1(side)", language="eng")
]
_SUCCESS = PipelineResult(outcome=PipelineOutcome.SUCCESS, success=True, detail="ok")
_FAILED = PipelineResult(outcome=PipelineOutcome.REMUX_FAILED, success=False, detail="killed")


def _surround_probe(_path: Path) -> Sequence[AudioStreamInfo]:
    return _SURROUND


@pytest.fixture
def session_factory(settings: Settings) -> Iterator[sessionmaker[Session]]:
    """A schema-initialised session factory over the isolated ``settings`` DB.

    Mirrors ``tests/test_jobs_scheduler.py``'s fixture of the same name --
    :class:`~collapsarr.jobs.scheduler.JobScheduler` (needed to exercise
    :func:`~collapsarr.self_update.apply.apply_with_flow`'s real requeue
    path) takes a ``sessionmaker``, not a bare :class:`Session`.
    """
    engine = create_engine_from_settings(settings)
    upgrade_to_head(settings)
    yield create_session_factory(engine)
    engine.dispose()


class _FakeProcess:
    """A stand-in for a job's live subprocess, killable by the cancel handle.

    Mirrors ``tests/test_jobs_queue.py``'s ``_FakeProcess`` exactly.
    """

    def __init__(self) -> None:
        self.pid = 2_000_000_000  # no such process -> os.getpgid raises, forcing kill()
        self.killed = threading.Event()

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:
        self.killed.set()


class _HardKillOnceRunner:
    """A pipeline_runner whose ``victim`` job blocks until killed -- once.

    Mirrors ``tests/test_jobs_queue.py``'s ``_HardKillRunner``, but only the
    *first* call for ``victim`` blocks/attaches a cancel handle -- a second
    call (the requeued Job "Cancel & Restart Now" creates) runs straight to
    success, standing in for the retried attempt genuinely completing this
    time.
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
            return _FAILED
        return _SUCCESS


class _GatedRunner:
    """A pipeline_runner whose ``victim`` job blocks until released, then succeeds.

    Mirrors ``tests/test_jobs_queue.py``'s ``_GatedRunner`` -- the "Wait &
    Restart" counterpart of :class:`_HardKillOnceRunner`: nothing here is
    ever killed, it just runs to completion once the test releases it.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, file_path: Path, _settings: DownmixSettings, **_: object) -> PipelineResult:
        if file_path.stem == "victim":
            self.started.set()
            assert self.release.wait(timeout=5), "victim job was never released"
        return _SUCCESS


def _live_pause_check(session_factory: sessionmaker[Session]) -> Callable[[], bool]:
    """A real, DB-backed ``pause_check`` (mirrors ``JobQueue.from_settings``'s own).

    Without this, the test :class:`JobQueue` below would default to
    ``pause_check=None`` ("never paused"), so
    :func:`~collapsarr.settings.service.stash_and_force_pause_processing`'s
    write to ``GlobalSettings.auto_processing_paused`` would have no
    observable effect at all on the queue -- a free worker would keep
    claiming ``PENDING`` Jobs (including a "Cancel & Restart Now" requeue)
    regardless of the force-pause, making these tests non-deterministic
    about exactly the behaviour they're meant to prove.
    """

    def _check() -> bool:
        with session_factory() as session:
            return get_global_settings(session).auto_processing_paused

    return _check


def _ensure_singleton_rows_exist(session_factory: sessionmaker[Session]) -> None:
    """Pre-create the ``GlobalSettings``/``SelfUpdateState`` singleton rows (COL-230-era race).

    Both :func:`~collapsarr.settings.service.get_global_settings` and
    :func:`~collapsarr.self_update.service.get_self_update_state` are
    get-or-create: a ``SELECT``, then an ``INSERT`` only if nothing came
    back. That's safe for a single caller, but every ``JobQueue`` this
    module builds with a real ``pause_check`` (:func:`_live_pause_check`)
    starts calling ``get_global_settings`` from its worker thread the
    instant :meth:`~collapsarr.jobs.queue.JobQueue.start` runs (every
    ``_claim_next`` poll reads it) -- concurrently with whatever the main
    test thread does next (``scheduler.trigger_file``'s own dedup-window
    read, ``apply_with_flow``'s pause stash, a second racing request's own
    guard check, ...). If neither row exists yet, two concurrent first
    reads can each see "nothing there" and both attempt the ``INSERT``,
    raising a raw ``sqlite3.IntegrityError`` instead of either call
    genuinely creating the row. Calling both get-or-create functions once,
    synchronously, before any concurrency starts (in particular, before
    :meth:`~collapsarr.jobs.queue.JobQueue.start`) closes that window for
    every test in this module that would otherwise be racing on a
    still-empty database.
    """
    with session_factory() as session:
        get_global_settings(session)
        get_self_update_state(session)


def _make_queue_and_scheduler(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    pipeline_runner: PipelineRunner,
) -> tuple[JobQueue, JobScheduler]:
    """A real, started :class:`JobQueue` + :class:`JobScheduler` pair (COL-233 tests).

    ``history_recorder`` is wired to the real DB (unlike most
    ``test_jobs_queue.py`` fixtures) so a hard-cancelled Job's ``FAILED``
    completion is genuinely persisted to ``JobHistory`` -- needed to prove
    :func:`~collapsarr.self_update.apply.apply_with_flow`'s "Cancel &
    Restart Now" requeue genuinely bypasses the Recently-Processed Window
    (COL-167) rather than the window simply never having anything to trip on.
    ``pause_check`` is likewise wired to the real DB (see
    :func:`_live_pause_check`) so Auto-Processing Pause genuinely gates
    claiming, matching production (:meth:`JobQueue.from_settings`) rather
    than the raw ``__init__`` default most other ``test_jobs_queue.py``
    fixtures rely on. See :func:`_ensure_singleton_rows_exist` for why the
    singleton settings/self-update rows are pre-created before the pool
    starts.
    """
    _ensure_singleton_rows_exist(session_factory)
    queue = JobQueue(
        max_concurrency=1,
        pipeline_runner=pipeline_runner,
        history_recorder=make_history_recorder(session_factory),
        pause_check=_live_pause_check(session_factory),
    )
    queue.start()
    scheduler = JobScheduler(queue, session_factory, settings, probe=_surround_probe)
    return queue, scheduler


def _make_queue_and_scheduler_without_history(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    pipeline_runner: PipelineRunner,
) -> tuple[JobQueue, JobScheduler]:
    """Like :func:`_make_queue_and_scheduler`, but without a ``history_recorder``.

    For tests that need a genuinely ``RUNNING`` (or force-paused-``PENDING``)
    Job in the live queue, but make no assertion about a persisted
    ``JobHistory`` row -- the large majority of this module's
    ``apply_with_flow`` end-to-end tests. Skipping ``history_recorder``
    avoids a real, pre-existing (COL-230-era) race between
    :meth:`~collapsarr.jobs.queue.JobQueue._enqueue` (which wakes a waiting
    worker *before* writing the enqueued Job's ``PENDING`` history row) and
    :func:`~collapsarr.jobs.history.record_job_history`'s non-atomic
    select-then-insert: under real thread contention a worker can start
    recording the same Job's ``RUNNING`` transition concurrently with that
    still-in-flight ``PENDING`` write, and the two race to insert the same
    ``job_id``, occasionally raising a raw ``IntegrityError`` instead of
    either write cleanly succeeding. That race is out of this ticket's
    scope to fix (a shared, heavily-used Job Queue module well beyond
    COL-233's mandate) -- this is the workaround
    :func:`test_apply_with_flow_two_genuinely_concurrent_calls_reject_the_second_before_any_stash`
    already uses. Only the "Cancel & Restart Now" requeue-bypasses-the-window
    test (which needs a *real* recent completion persisted, not merely an
    absent one, to prove the bypass matters) still uses
    :func:`_make_queue_and_scheduler` instead. See
    :func:`_ensure_singleton_rows_exist` for the *other*, unrelated
    get-or-create race this also closes.
    """
    _ensure_singleton_rows_exist(session_factory)
    queue = JobQueue(
        max_concurrency=1,
        pipeline_runner=pipeline_runner,
        pause_check=_live_pause_check(session_factory),
    )
    queue.start()
    scheduler = JobScheduler(queue, session_factory, settings, probe=_surround_probe)
    return queue, scheduler


def _call_apply_with_flow(
    session: Session,
    *,
    flow: str,
    queue: JobQueue,
    scheduler: JobScheduler | None = None,
    transport: httpx.MockTransport | None = None,
    wait_timeout: float = WAIT_AND_RESTART_TIMEOUT,
    cancel_drain_timeout: float = CANCEL_DRAIN_TIMEOUT,
) -> SelfUpdateApplyOutcome:
    """Call :func:`apply_with_flow` with this module's shared, deterministic seams.

    ``target_tag``/``subprocess_runner``/``reexec_fn`` are always the same
    fixed test doubles this file already uses for :func:`apply_pipx_update`
    itself; only ``flow``/``queue``/``scheduler``/``transport``/the two
    timeouts vary per test.
    """
    return apply_with_flow(
        session,
        target_tag=_TAG,
        flow=flow,
        queue=queue,
        scheduler=scheduler,
        transport=transport or _transport(),
        subprocess_runner=_RunnerSpy(returncode=0),
        reexec_fn=_ReexecSpy(),
        wait_timeout=wait_timeout,
        cancel_drain_timeout=cancel_drain_timeout,
    )


# --------------------------------------------------------------------------- #
# Argument validation
# --------------------------------------------------------------------------- #


def test_apply_with_flow_rejects_an_unknown_flow(session: Session) -> None:
    queue = JobQueue(pipeline_runner=lambda *a, **k: _SUCCESS)
    with pytest.raises(ValueError, match="Unknown self-update flow"):
        _call_apply_with_flow(session, flow="not_a_real_flow", queue=queue)


def test_apply_with_flow_requires_a_scheduler_for_cancel_and_restart(session: Session) -> None:
    queue = JobQueue(pipeline_runner=lambda *a, **k: _SUCCESS)
    with pytest.raises(ValueError, match="scheduler"):
        _call_apply_with_flow(
            session, flow=FLOW_CANCEL_AND_RESTART, queue=queue, scheduler=None
        )


def test_apply_with_flow_rejects_a_concurrent_trigger_before_touching_pause(
    session: Session,
) -> None:
    """The real guard is reserved before Auto-Processing Pause is ever touched."""
    begin_self_update(session, previous_version="0.9.0")
    queue = JobQueue(pipeline_runner=lambda *a, **k: _SUCCESS)

    with pytest.raises(SelfUpdateAlreadyInProgressError):
        _call_apply_with_flow(session, flow=FLOW_WAIT_AND_RESTART, queue=queue)

    settings_row = get_global_settings(session)
    assert settings_row.auto_processing_paused is False  # never forced
    assert settings_row.auto_processing_pause_restore_value is None  # never stashed


def test_apply_with_flow_two_genuinely_concurrent_calls_reject_the_second_before_any_stash(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code-review must-fix regression test: two apply_with_flow calls racing each other.

    Unlike :func:`test_apply_with_flow_rejects_a_concurrent_trigger_before_touching_pause`
    above (guard already held *before* ``apply_with_flow`` is even called --
    the easy, sequential case), this drives request A into its wait step
    first, confirms the guard is genuinely held mid-flight, and only *then*
    fires request B -- proving the guard is reserved before the cancel/wait
    phase begins, not after it (which would leave the whole phase -- up to
    ``wait_timeout``/``cancel_drain_timeout`` wide -- open to a second
    request corrupting the pause-restore stash; see ``apply_with_flow``'s own
    docstring, step 2).
    """
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _GatedRunner()  # request A's victim Job blocks until released
    # _make_queue_and_scheduler_without_history, not _make_queue_and_scheduler:
    # this test doesn't need persisted JobHistory rows at all -- see that
    # helper's own docstring for why wiring one would be actively harmful
    # here (a separate, pre-existing race it works around).
    queue, scheduler = _make_queue_and_scheduler_without_history(
        settings, session_factory, pipeline_runner=runner
    )
    try:
        # No need to pre-create the singleton self_update_state row here --
        # _make_queue_and_scheduler_without_history already does (see
        # _ensure_singleton_rows_exist), so both requests below race on the
        # UPDATE path this test actually cares about, not on who wins the
        # row's first-ever creation.
        victim = scheduler.trigger_file("/media/victim.mkv")
        assert victim is not None
        assert runner.started.wait(timeout=5)

        result_holder: dict[str, SelfUpdateApplyOutcome] = {}

        def _run_request_a() -> None:
            with session_factory() as session:
                result_holder["outcome"] = _call_apply_with_flow(
                    session, flow=FLOW_WAIT_AND_RESTART, queue=queue
                )

        thread = threading.Thread(target=_run_request_a)
        thread.start()

        # Wait for request A to have genuinely reserved the guard (and, by
        # extension, forced the pause) before firing request B -- otherwise
        # this test isn't actually racing anything.
        in_progress = False
        for _ in range(50):
            with session_factory() as poll_session:
                in_progress = get_self_update_state(poll_session).in_progress
            if in_progress:
                break
            time.sleep(0.05)
        assert in_progress is True

        # Request B: fired while A is still blocked in its own wait step.
        # Must be rejected immediately by the real guard -- before it ever
        # calls stash_and_force_pause_processing itself.
        with session_factory() as session_b:
            with pytest.raises(SelfUpdateAlreadyInProgressError):
                _call_apply_with_flow(session_b, flow=FLOW_WAIT_AND_RESTART, queue=queue)

        runner.release.set()  # let request A's victim Job finish naturally
        thread.join(timeout=5)
        assert not thread.is_alive()

        outcome = result_holder["outcome"]
        assert outcome.ok is True, outcome.error

        # The must-fix this test guards against: request B must never have
        # overwritten request A's correct stash (auto_processing_paused was
        # False before either request ran).
        with session_factory() as session:
            row = get_global_settings(session)
            assert row.auto_processing_paused is True
            assert row.auto_processing_pause_restore_value is False
    finally:
        queue.shutdown()


# --------------------------------------------------------------------------- #
# AC: "Cancel & Restart Now" end to end with a RUNNING Job.
# --------------------------------------------------------------------------- #


def test_apply_with_flow_cancel_and_restart_hard_kills_and_requeues_bypassing_the_window(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _HardKillOnceRunner()
    queue, scheduler = _make_queue_and_scheduler(settings, session_factory, pipeline_runner=runner)
    try:
        victim = scheduler.trigger_file("/media/victim.mkv")
        assert victim is not None
        assert runner.started.wait(timeout=5)  # worker claimed + is "running" it

        # Spy on trigger_file to prove the requeue genuinely passes
        # bypass_dedup_window=True -- the AC's explicit ask -- rather than
        # just inferring it from the requeued Job existing.
        calls: list[tuple[Path, bool]] = []
        original_trigger_file = scheduler.trigger_file

        def _spy_trigger_file(
            file_path: str | Path,
            *,
            extra_languages: Iterable[str] | None = None,
            session: Session | None = None,
            bypass_dedup_window: bool = False,
        ) -> Job | None:
            calls.append((Path(file_path), bypass_dedup_window))
            return original_trigger_file(
                file_path,
                extra_languages=extra_languages,
                session=session,
                bypass_dedup_window=bypass_dedup_window,
            )

        scheduler.trigger_file = _spy_trigger_file  # type: ignore[method-assign]

        with session_factory() as session:
            outcome = _call_apply_with_flow(
                session, flow=FLOW_CANCEL_AND_RESTART, queue=queue, scheduler=scheduler
            )

            assert outcome.ok is True, outcome.error

            # Hard-cancel: the subprocess was killed, and the victim Job
            # failed out through the ordinary terminal path (no new terminal
            # status -- COL-233's own AC).
            assert runner.process.killed.is_set()
            assert victim.status is JobStatus.FAILED

            # Requeue: bypass_dedup_window=True genuinely reached trigger_file
            # for the cancelled Job's file...
            assert calls == [(Path("/media/victim.mkv"), True)]
            # ...and a *second*, distinct Job for that same file now exists --
            # proof the bypass actually let it through despite the victim's
            # own FAILED completion (recorded moments ago, well inside the
            # 360-minute default Recently-Processed Window) having just been
            # persisted to JobHistory.
            requeued = [
                job
                for job in queue.list_jobs()
                if job.file_path == Path("/media/victim.mkv") and job.id != victim.id
            ]
            assert len(requeued) == 1

            # Both flows force-pause Auto-Processing Pause for the duration,
            # stashing the pre-update value -- here, False (nothing was
            # manually paused before this attempt).
            row = get_global_settings(session)
            assert row.auto_processing_paused is True
            assert row.auto_processing_pause_restore_value is False

            # ok=True (pipx upgrade "succeeded", re-exec is imminent) leaves
            # the pause forced -- there is no restart in *this* process to
            # hang the restore off, so it's the next boot's job.
    finally:
        queue.shutdown()


def test_apply_with_flow_cancel_and_restart_restores_pause_when_the_apply_itself_fails(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure *after* Jobs are cleared (checksum mismatch) still restores the pause.

    Unlike the happy path, this process is not about to restart -- so the
    pause this attempt forced must not linger; :func:`apply_with_flow`
    restores it inline rather than waiting for a boot that may be a long way
    off (or never come, if the operator just gives up and retries later).
    """
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _HardKillOnceRunner()
    # _make_queue_and_scheduler_without_history: this test asserts on the
    # pause/guard outcome only, not on any persisted JobHistory row -- see
    # that helper's own docstring for why history_recorder is skipped here.
    queue, scheduler = _make_queue_and_scheduler_without_history(
        settings, session_factory, pipeline_runner=runner
    )
    try:
        victim = scheduler.trigger_file("/media/victim.mkv")
        assert victim is not None
        assert runner.started.wait(timeout=5)

        with session_factory() as session:
            wrong_sha256sums = f"{'0' * 64}  {_FILENAME}\n"
            outcome = _call_apply_with_flow(
                session,
                flow=FLOW_CANCEL_AND_RESTART,
                queue=queue,
                scheduler=scheduler,
                transport=_transport(sha256sums=wrong_sha256sums),
            )

            assert outcome.ok is False

            row = get_global_settings(session)
            assert row.auto_processing_paused is False  # restored -- nothing stuck paused
            assert row.auto_processing_pause_restore_value is None
    finally:
        queue.shutdown()


# --------------------------------------------------------------------------- #
# AC: "Wait & Restart" end to end with a RUNNING Job.
# --------------------------------------------------------------------------- #


def test_apply_with_flow_wait_and_restart_blocks_then_proceeds_without_cancelling(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _GatedRunner()
    # _make_queue_and_scheduler_without_history: no assertion here touches a
    # persisted JobHistory row -- see that helper's own docstring.
    queue, scheduler = _make_queue_and_scheduler_without_history(
        settings, session_factory, pipeline_runner=runner
    )
    try:
        victim = scheduler.trigger_file("/media/victim.mkv")
        assert victim is not None
        assert runner.started.wait(timeout=5)  # worker claimed + is "running" it

        result_holder: dict[str, SelfUpdateApplyOutcome] = {}

        def _run_apply() -> None:
            with session_factory() as session:
                result_holder["outcome"] = _call_apply_with_flow(
                    session, flow=FLOW_WAIT_AND_RESTART, queue=queue
                )

        thread = threading.Thread(target=_run_apply)
        thread.start()

        # While the victim Job is still running, apply_with_flow must be
        # blocked in the wait step -- Auto-Processing Pause is already
        # forced (stashed before the wait), but the outcome hasn't landed
        # yet and nothing has been cancelled. Each poll opens a fresh
        # session -- reusing one would just keep returning its own
        # already-cached (and so stale) row rather than seeing the other
        # thread's committed write.
        paused = False
        for _ in range(50):
            with session_factory() as poll_session:
                paused = get_global_settings(poll_session).auto_processing_paused
            if paused:
                break
            time.sleep(0.05)
        assert paused is True
        assert "outcome" not in result_holder
        status_while_waiting = victim.status
        assert status_while_waiting is JobStatus.RUNNING  # never hard-cancelled

        runner.release.set()  # let the victim Job finish naturally
        thread.join(timeout=5)
        assert not thread.is_alive()

        outcome = result_holder["outcome"]
        assert outcome.ok is True, outcome.error
        status_after_finishing = victim.status
        assert status_after_finishing is JobStatus.SUCCEEDED  # finished naturally, never FAILED
        # Nothing extra was enqueued for this file -- "Wait & Restart" never
        # requeues anything.
        assert [j for j in queue.list_jobs() if j.file_path == Path("/media/victim.mkv")] == [
            victim
        ]

        with session_factory() as session:
            row = get_global_settings(session)
            assert row.auto_processing_paused is True
            assert row.auto_processing_pause_restore_value is False
    finally:
        queue.shutdown()


def test_apply_with_flow_wait_and_restart_times_out_and_restores_pause(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _GatedRunner()  # never released -- the wait must time out
    # _make_queue_and_scheduler_without_history: no assertion here touches a
    # persisted JobHistory row -- see that helper's own docstring.
    queue, scheduler = _make_queue_and_scheduler_without_history(
        settings, session_factory, pipeline_runner=runner
    )
    try:
        # Trigger + wait for the victim Job to actually be claimed *before*
        # introducing the pre-existing manual pause below -- otherwise the
        # live pause_check would stop it ever being claimed at all.
        victim = scheduler.trigger_file("/media/victim.mkv")
        assert victim is not None
        assert runner.started.wait(timeout=5)

        # A pre-existing *manual* pause -- must still read back as paused
        # after apply_with_flow restores it (nothing self-update-induced
        # should ever clear a manual pause). Set only now that the victim
        # Job is already RUNNING (pause only ever gates *claiming*, never an
        # in-flight Job -- see GlobalSettings.auto_processing_paused).
        with session_factory() as seed_session:
            update_global_settings(seed_session, auto_processing_paused=True)

        with session_factory() as session:
            outcome = _call_apply_with_flow(
                session, flow=FLOW_WAIT_AND_RESTART, queue=queue, wait_timeout=0.2
            )

            assert outcome.ok is False
            assert outcome.error is not None
            assert "timed out" in outcome.error.lower()

            row = get_global_settings(session)
            # Restored to the pre-existing manual pause -- True -- not left
            # forced, and not wrongly reset to False either.
            assert row.auto_processing_paused is True
            assert row.auto_processing_pause_restore_value is None

        # The Self-Update in-progress guard -- reserved by apply_with_flow
        # itself before the wait even started -- was cleared back to idle by
        # its own failure handling once the wait timed out: a fresh attempt
        # is free to start.
        with session_factory() as check_session:
            state = get_self_update_state(check_session)
            assert state.in_progress is False
    finally:
        runner.release.set()  # unblock the victim Job so shutdown() doesn't hang
        queue.shutdown()


# --------------------------------------------------------------------------- #
# AC: restore-value stash/consume across a simulated restart.
# --------------------------------------------------------------------------- #


def test_apply_with_flow_success_then_simulated_restart_preserves_a_manual_pause(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    update_global_settings(session, auto_processing_paused=True)  # pre-existing manual pause
    queue = JobQueue(pipeline_runner=lambda *a, **k: _SUCCESS)

    outcome = _call_apply_with_flow(session, flow=FLOW_WAIT_AND_RESTART, queue=queue)
    assert outcome.ok is True, outcome.error

    row = get_global_settings(session)
    assert row.auto_processing_paused is True  # still forced (was already True)
    assert row.auto_processing_pause_restore_value is True  # stashed the manual pause

    # Simulated restart: the boot-time consumer (collapsarr.main's lifespan)
    # calls exactly this, directly, rather than spinning up a real process.
    from collapsarr.settings.service import restore_auto_processing_pause

    restore_auto_processing_pause(session)

    row = get_global_settings(session)
    assert row.auto_processing_paused is True  # manual pause survives the restart
    assert row.auto_processing_pause_restore_value is None


def test_apply_with_flow_success_then_simulated_restart_resumes_processing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    # auto_processing_paused defaults False -- no manual pause beforehand.
    queue = JobQueue(pipeline_runner=lambda *a, **k: _SUCCESS)

    outcome = _call_apply_with_flow(session, flow=FLOW_WAIT_AND_RESTART, queue=queue)
    assert outcome.ok is True, outcome.error

    row = get_global_settings(session)
    assert row.auto_processing_paused is True  # forced by this apply attempt
    assert row.auto_processing_pause_restore_value is False  # stashed the pre-update False

    from collapsarr.settings.service import restore_auto_processing_pause

    restore_auto_processing_pause(session)

    row = get_global_settings(session)
    assert row.auto_processing_paused is False  # resumes -- this pause was never manual
    assert row.auto_processing_pause_restore_value is None
