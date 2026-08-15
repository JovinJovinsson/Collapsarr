"""Tests for the post-re-exec health-check gate + pinned-reinstall rollback
(COL-234) and its native swap-back rollback counterpart (COL-236).

Most of this file exercises the bare service-layer ``session`` fixture (no
HTTP app, no FastAPI lifespan), mirroring ``tests/test_self_update_apply.py``'s
convention -- ``resolve_awaiting_health`` has no FastAPI dependency of its
own (see ``collapsarr.self_update.health_gate``'s module docstring for why
it is wired into the lifespan rather than exposed as its own endpoint).
Every OS-touching seam (``health_check_fn``, ``subprocess_runner``,
``reexec_fn``, ``sleep_fn``, and -- for COL-236 -- ``native_reexec_fn``) is a
fake/spy throughout -- no real subprocess spawn, re-exec, or wall-clock sleep
ever happens in this file. The one exception is ``native_swap_fn``, which the
COL-236 tests below mostly leave as the real
``collapsarr.self_update.native.swap_install_dir`` (a pure, already-tested
filesystem primitive, per that module's own test suite) operating on
``tmp_path`` directories standing in for the install/backup dirs -- exactly
what actually proves the swap-back's on-disk postconditions, mirroring
``tests/test_self_update_native.py``'s own convention. The final section
exercises the real ``collapsarr.main`` wiring end to end (an
``awaiting_health`` row seeded before the app boots, a fake
``self_update_health_check_fn`` on ``create_app``), confirming the gate
actually runs from the lifespan and that ``GET
/api/system/self-update/status`` reflects the rolled-back state -- no
``routes.py`` changes were needed for this (its response shape already
echoes ``phase``/``previous_version`` verbatim).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.main import create_app
from collapsarr.migrations import upgrade_to_head
from collapsarr.self_update.health_gate import (
    SelfUpdateHealthGateOutcome,
    resolve_awaiting_health,
)
from collapsarr.self_update.models import PHASE_AWAITING_HEALTH, PHASE_IDLE, PHASE_ROLLED_BACK
from collapsarr.self_update.native import (
    NATIVE_BACKUP_DIRNAME,
    NATIVE_QUARANTINE_DIRNAME,
    swap_install_dir,
)
from collapsarr.self_update.service import (
    begin_self_update,
    get_self_update_state,
    set_self_update_phase,
)
from collapsarr.settings.service import get_global_settings
from collapsarr.system.info import INSTALL_METHOD_NATIVE, INSTALL_METHOD_PIPX


class _HealthCheckSpy:
    """A fake :data:`~collapsarr.self_update.health_gate.HealthCheckFn`.

    Returns ``False`` for the first ``fail_count`` calls, then ``True`` --
    ``fail_count=None`` (the default) never reports healthy, exercising the
    "never becomes healthy within the attempt budget" rollback path.
    """

    def __init__(self, *, fail_count: int | None = None) -> None:
        self.fail_count = fail_count
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        if self.fail_count is None:
            return False
        return self.calls > self.fail_count


class _RunnerSpy:
    """A fake :data:`~collapsarr.self_update.apply.SubprocessRunner`.

    Records every call and returns a fixed :class:`subprocess.CompletedProcess`
    -- never spawns a real ``pip`` process.
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


class _SleepSpy:
    """A fake ``sleep_fn`` -- records every requested duration, never really sleeps."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _NativeReexecSpy:
    """A fake :data:`~collapsarr.self_update.native.ReexecFn` (COL-236).

    Records the ``live_dir`` it was called with and returns normally -- a
    real ``os.execv`` never returns.
    """

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __call__(self, live_dir: Path) -> None:
        self.calls.append(live_dir)


def _write_native_install(root: Path, marker_name: str, content: bytes) -> None:
    """Mirrors ``tests/test_self_update_native.py``'s own ``_write_install``
    helper -- a tiny marker file standing in for a whole install tree."""
    root.mkdir(parents=True, exist_ok=True)
    (root / marker_name).write_bytes(content)


# --------------------------------------------------------------------------- #
# AC: not awaiting_health -- a no-op on every ordinary boot
# --------------------------------------------------------------------------- #


def test_resolve_awaiting_health_is_a_noop_when_idle(session: Session) -> None:
    health_check = _HealthCheckSpy()
    runner = _RunnerSpy()
    reexec = _ReexecSpy()

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        subprocess_runner=runner,
        reexec_fn=reexec,
        sleep_fn=_SleepSpy(),
    )

    assert outcome == SelfUpdateHealthGateOutcome(action="noop")
    assert health_check.calls == 0
    assert runner.calls == []
    assert reexec.calls == 0


def test_resolve_awaiting_health_is_a_noop_for_any_other_phase(session: Session) -> None:
    begin_self_update(session, previous_version="1.0.0")
    set_self_update_phase(session, "verifying")
    health_check = _HealthCheckSpy()

    outcome = resolve_awaiting_health(
        session, health_check_fn=health_check, sleep_fn=_SleepSpy()
    )

    assert outcome.action == "noop"
    assert health_check.calls == 0


# --------------------------------------------------------------------------- #
# AC: successful health check leaves state uncorrupted -- guard cleared
# normally, no reinstall/relaunch triggered.
# --------------------------------------------------------------------------- #


def test_resolve_awaiting_health_clears_the_guard_on_an_immediate_healthy_check(
    session: Session,
) -> None:
    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=0)  # healthy on the very first call
    runner = _RunnerSpy()
    reexec = _ReexecSpy()
    sleep = _SleepSpy()

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        subprocess_runner=runner,
        reexec_fn=reexec,
        sleep_fn=sleep,
    )

    assert outcome == SelfUpdateHealthGateOutcome(action="healthy")
    assert health_check.calls == 1
    assert sleep.calls == []

    # No reinstall/relaunch triggered.
    assert runner.calls == []
    assert reexec.calls == 0

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE
    assert state.previous_version == "1.0.0"  # left in place, not cleared


def test_resolve_awaiting_health_tolerates_a_transient_failure_before_passing(
    session: Session,
) -> None:
    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=2)  # unhealthy twice, then healthy
    runner = _RunnerSpy()
    reexec = _ReexecSpy()
    sleep = _SleepSpy()

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        subprocess_runner=runner,
        reexec_fn=reexec,
        sleep_fn=sleep,
        max_attempts=6,
        poll_interval=5.0,
    )

    assert outcome.action == "healthy"
    assert health_check.calls == 3
    assert sleep.calls == [5.0, 5.0]  # slept between the two failed attempts only
    assert runner.calls == []
    assert reexec.calls == 0

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


# --------------------------------------------------------------------------- #
# AC: timeout-triggered rollback -- pinned reinstall happens, relaunch
# happens, status reflects the rolled-back state.
# --------------------------------------------------------------------------- #


def test_resolve_awaiting_health_rolls_back_after_the_health_check_never_passes(
    session: Session,
) -> None:
    begin_self_update(session, previous_version="1.2.3", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=None)  # never becomes healthy
    runner = _RunnerSpy(returncode=0)
    reexec = _ReexecSpy()
    sleep = _SleepSpy()

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        subprocess_runner=runner,
        reexec_fn=reexec,
        sleep_fn=sleep,
        max_attempts=3,
        poll_interval=5.0,
    )

    assert outcome == SelfUpdateHealthGateOutcome(action="rolled_back")
    assert health_check.calls == 3
    assert sleep.calls == [5.0, 5.0]  # never a trailing sleep after the last attempt

    # Pinned reinstall happened, targeting the exact previous version.
    assert len(runner.calls) == 1
    command, _timeout = runner.calls[0]
    assert tuple(command) == ("pip", "install", "collapsarr==1.2.3")

    # Relaunch happened.
    assert reexec.calls == 1

    # Status reflects the rolled-back state.
    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_ROLLED_BACK
    assert state.previous_version == "1.2.3"


# --------------------------------------------------------------------------- #
# Rollback itself can fail -- never leaves the guard stuck (mirrors COL-232's
# own "no failure can leave the guard stuck" fix-loop precedent).
# --------------------------------------------------------------------------- #


def test_resolve_awaiting_health_clears_the_guard_when_pip_is_missing(session: Session) -> None:
    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=None)

    def _missing_pip(
        command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("pip not found")

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        subprocess_runner=_missing_pip,
        reexec_fn=_ReexecSpy(),
        sleep_fn=_SleepSpy(),
        max_attempts=1,
    )

    assert outcome.action == "rollback_failed"
    assert outcome.error is not None
    assert "pip" in outcome.error.lower()

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE

    # The guard genuinely recovered: a fresh attempt can begin.
    begin_self_update(session, previous_version="1.1.0")


def test_resolve_awaiting_health_clears_the_guard_on_a_failed_reinstall_exit(
    session: Session,
) -> None:
    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=None)
    runner = _RunnerSpy(returncode=1, stderr="ERROR: No matching distribution found")
    reexec = _ReexecSpy()

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        subprocess_runner=runner,
        reexec_fn=reexec,
        sleep_fn=_SleepSpy(),
        max_attempts=1,
    )

    assert outcome.action == "rollback_failed"
    assert outcome.error is not None
    assert "exit 1" in outcome.error
    assert reexec.calls == 0  # never relaunched into a failed reinstall

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


def test_resolve_awaiting_health_clears_the_guard_on_a_reinstall_timeout(
    session: Session,
) -> None:
    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=None)

    def _timing_out(
        command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=list(command), timeout=timeout)

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        subprocess_runner=_timing_out,
        reexec_fn=_ReexecSpy(),
        sleep_fn=_SleepSpy(),
        max_attempts=1,
    )

    assert outcome.action == "rollback_failed"
    assert outcome.error is not None
    assert "timed out" in outcome.error.lower()

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


def test_resolve_awaiting_health_clears_the_guard_when_no_previous_version_is_recorded(
    session: Session,
) -> None:
    # Directly force the phase without ever recording a previous_version --
    # a defensive edge case that should never happen via the real
    # apply_pipx_update flow (it always stamps previous_version first), but
    # must still not leave the guard stuck if it somehow does.
    row = get_self_update_state(session)
    row.in_progress = True
    row.phase = PHASE_AWAITING_HEALTH
    session.commit()

    health_check = _HealthCheckSpy(fail_count=None)
    runner = _RunnerSpy()
    reexec = _ReexecSpy()

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        subprocess_runner=runner,
        reexec_fn=reexec,
        sleep_fn=_SleepSpy(),
        max_attempts=1,
    )

    assert outcome.action == "rollback_failed"
    assert runner.calls == []
    assert reexec.calls == 0

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


def test_resolve_awaiting_health_clears_the_guard_on_an_unexpected_health_check_exception(
    session: Session,
) -> None:
    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)

    def _exploding_health_check() -> bool:
        raise RuntimeError("boom")

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=_exploding_health_check,
        subprocess_runner=_RunnerSpy(),
        reexec_fn=_ReexecSpy(),
        sleep_fn=_SleepSpy(),
        max_attempts=1,
    )

    assert outcome.action == "rollback_failed"

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


# --------------------------------------------------------------------------- #
# Default seams: production defaults are used when not overridden.
# --------------------------------------------------------------------------- #


def test_resolve_awaiting_health_uses_default_subprocess_runner_and_reexec_when_unset(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=None)

    run_calls: list[tuple[Sequence[str], float]] = []
    reexec_calls: list[None] = []

    def _fake_run(
        command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        run_calls.append((command, timeout))
        return subprocess.CompletedProcess(list(command), 0, stdout="", stderr="")

    def _fake_reexec() -> None:
        reexec_calls.append(None)

    monkeypatch.setattr("collapsarr.self_update.health_gate._run_pip_install", _fake_run)
    monkeypatch.setattr("collapsarr.self_update.health_gate._relaunch_process", _fake_reexec)

    outcome = resolve_awaiting_health(
        session, health_check_fn=health_check, sleep_fn=_SleepSpy(), max_attempts=1
    )

    assert outcome.action == "rolled_back"
    assert len(run_calls) == 1
    assert len(reexec_calls) == 1


# --------------------------------------------------------------------------- #
# COL-236: native swap-back rollback -- the retained folder is swapped back
# in on timeout, relaunched, and only ever deleted once the new build is
# proven healthy.
# --------------------------------------------------------------------------- #


def test_resolve_awaiting_health_swaps_back_the_native_install_after_timeout(
    session: Session, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    backup_dir = tmp_path / NATIVE_BACKUP_DIRNAME
    _write_native_install(install_dir, "marker.txt", b"NEW-UNHEALTHY")
    _write_native_install(backup_dir, "marker.txt", b"OLD-HEALTHY")

    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=None)  # never becomes healthy
    reexec = _NativeReexecSpy()
    sleep = _SleepSpy()

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        install_method_fn=lambda: INSTALL_METHOD_NATIVE,
        native_install_dir=install_dir,
        native_swap_fn=swap_install_dir,  # the real, already-tested primitive
        native_reexec_fn=reexec,
        sleep_fn=sleep,
        max_attempts=3,
        poll_interval=5.0,
    )

    assert outcome == SelfUpdateHealthGateOutcome(action="rolled_back")
    assert health_check.calls == 3
    assert sleep.calls == [5.0, 5.0]

    # The old (pre-update) folder is swapped back into live_dir...
    assert (install_dir / "marker.txt").read_bytes() == b"OLD-HEALTHY"
    # ...and the new/unhealthy build is no longer at live_dir, nor left
    # anywhere else on disk (quarantined, then deleted, once the swap-back
    # committed).
    assert not backup_dir.exists()
    assert not (tmp_path / NATIVE_QUARANTINE_DIRNAME).exists()

    # Relaunch triggered, into the restored live_dir.
    assert reexec.calls == [install_dir]

    # Status reflects the rolled-back state, consistent with the pipx shape.
    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_ROLLED_BACK
    assert state.previous_version == "1.0.0"


def test_resolve_awaiting_health_deletes_the_retained_folder_on_a_healthy_native_check(
    session: Session, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    backup_dir = tmp_path / NATIVE_BACKUP_DIRNAME
    _write_native_install(install_dir, "marker.txt", b"NEW-HEALTHY")
    _write_native_install(backup_dir, "marker.txt", b"OLD")

    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=0)  # healthy on the first call
    reexec = _NativeReexecSpy()

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        install_method_fn=lambda: INSTALL_METHOD_NATIVE,
        native_install_dir=install_dir,
        native_swap_fn=swap_install_dir,
        native_reexec_fn=reexec,
        sleep_fn=_SleepSpy(),
    )

    assert outcome == SelfUpdateHealthGateOutcome(action="healthy")
    # The retained backup is deleted now that the new build is proven healthy...
    assert not backup_dir.exists()
    # ...but the live (new, now-confirmed-good) install is untouched.
    assert (install_dir / "marker.txt").read_bytes() == b"NEW-HEALTHY"
    assert reexec.calls == []  # no rollback, no relaunch

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE
    assert state.previous_version == "1.0.0"  # left in place, not cleared


def test_resolve_awaiting_health_routes_a_native_boot_to_the_native_path_not_pipx(
    session: Session, tmp_path: Path
) -> None:
    """COL-236 regression: the integration gap this ticket closes -- a native
    install's ``awaiting_health`` boot must dispatch to the native swap-back
    rollback, never to the pipx pinned-reinstall path (which would try
    ``pip install`` against a frozen build with no venv and simply fail)."""
    install_dir = tmp_path / "install"
    backup_dir = tmp_path / NATIVE_BACKUP_DIRNAME
    _write_native_install(install_dir, "marker.txt", b"NEW")
    _write_native_install(backup_dir, "marker.txt", b"OLD")

    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=None)
    pipx_runner = _RunnerSpy()
    pipx_reexec = _ReexecSpy()
    native_reexec = _NativeReexecSpy()

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        install_method_fn=lambda: INSTALL_METHOD_NATIVE,
        native_install_dir=install_dir,
        native_swap_fn=swap_install_dir,
        native_reexec_fn=native_reexec,
        subprocess_runner=pipx_runner,
        reexec_fn=pipx_reexec,
        sleep_fn=_SleepSpy(),
        max_attempts=2,
        poll_interval=1.0,
    )

    assert outcome.action == "rolled_back"
    # The pipx pinned-reinstall path never ran.
    assert pipx_runner.calls == []
    assert pipx_reexec.calls == 0
    # Only the native swap-back path ran.
    assert native_reexec.calls == [install_dir]
    assert (install_dir / "marker.txt").read_bytes() == b"OLD"


def test_resolve_awaiting_health_does_not_route_a_pipx_boot_to_the_native_path(
    session: Session,
) -> None:
    """The reverse of the regression above -- a pipx (or any non-native)
    install must keep running the pinned-reinstall path even though
    ``install_method_fn`` is now consulted on every rollback."""
    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=None)
    runner = _RunnerSpy(returncode=0)
    reexec = _ReexecSpy()
    native_reexec = _NativeReexecSpy()

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        install_method_fn=lambda: INSTALL_METHOD_PIPX,
        subprocess_runner=runner,
        reexec_fn=reexec,
        native_reexec_fn=native_reexec,
        sleep_fn=_SleepSpy(),
        max_attempts=1,
    )

    assert outcome.action == "rolled_back"
    assert len(runner.calls) == 1
    assert reexec.calls == 1
    assert native_reexec.calls == []


def test_resolve_awaiting_health_clears_the_guard_when_no_native_backup_exists(
    session: Session, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    _write_native_install(install_dir, "marker.txt", b"NEW")
    # No retained backup directory -- nothing to roll back to.

    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=None)

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        install_method_fn=lambda: INSTALL_METHOD_NATIVE,
        native_install_dir=install_dir,
        native_reexec_fn=_NativeReexecSpy(),
        sleep_fn=_SleepSpy(),
        max_attempts=1,
    )

    assert outcome.action == "rollback_failed"
    assert outcome.error is not None
    assert "backup" in outcome.error.lower()

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE

    # The guard genuinely recovered: a fresh attempt can begin.
    begin_self_update(session, previous_version="1.1.0")


def test_resolve_awaiting_health_clears_the_guard_on_a_failed_native_swap(
    session: Session, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    backup_dir = tmp_path / NATIVE_BACKUP_DIRNAME
    _write_native_install(install_dir, "marker.txt", b"NEW")
    _write_native_install(backup_dir, "marker.txt", b"OLD")

    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=None)

    def _exploding_swap(live: Path, staged: Path, backup: Path) -> None:
        raise OSError("simulated locked install directory")

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        install_method_fn=lambda: INSTALL_METHOD_NATIVE,
        native_install_dir=install_dir,
        native_swap_fn=_exploding_swap,
        native_reexec_fn=_NativeReexecSpy(),
        sleep_fn=_SleepSpy(),
        max_attempts=1,
    )

    assert outcome.action == "rollback_failed"
    assert outcome.error is not None
    assert "swap" in outcome.error.lower()

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


def test_resolve_awaiting_health_uses_default_native_swap_and_reexec_when_unset(
    session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    backup_dir = tmp_path / NATIVE_BACKUP_DIRNAME
    _write_native_install(install_dir, "marker.txt", b"NEW")
    _write_native_install(backup_dir, "marker.txt", b"OLD")

    begin_self_update(session, previous_version="1.0.0", phase=PHASE_AWAITING_HEALTH)
    health_check = _HealthCheckSpy(fail_count=None)
    reexec_calls: list[Path] = []

    def _fake_reexec(live_dir: Path) -> None:
        reexec_calls.append(live_dir)

    # No native_install_dir override either -- the default resolve_install_dir()
    # seam is patched so this exercises the real production default wiring.
    monkeypatch.setattr(
        "collapsarr.self_update.health_gate.resolve_install_dir", lambda: install_dir
    )
    monkeypatch.setattr(
        "collapsarr.self_update.health_gate._native_relaunch_process", _fake_reexec
    )

    outcome = resolve_awaiting_health(
        session,
        health_check_fn=health_check,
        install_method_fn=lambda: INSTALL_METHOD_NATIVE,
        sleep_fn=_SleepSpy(),
        max_attempts=1,
    )

    assert outcome.action == "rolled_back"
    assert reexec_calls == [install_dir]
    # The real, unpatched swap_install_dir ran (no native_swap_fn override).
    assert (install_dir / "marker.txt").read_bytes() == b"OLD"


# --------------------------------------------------------------------------- #
# Integration: the real collapsarr.main lifespan wiring, end to end.
# --------------------------------------------------------------------------- #


def _offline_update_check_transport() -> httpx.MockTransport:
    """Mirrors ``conftest.py``'s private helper -- kept local, matching
    ``tests/test_self_update_routes.py``'s own convention."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="offline in tests")

    return httpx.MockTransport(handler)


def _seed_awaiting_health(settings: Settings, *, previous_version: str) -> None:
    """Persist an ``awaiting_health`` row *before* the app under test boots --
    simulating the boot immediately following COL-232's apply-flow re-exec.
    """
    upgrade_to_head(settings)
    engine = create_engine_from_settings(settings)
    session_factory = create_session_factory(engine)
    with session_factory() as seed_session:
        begin_self_update(
            seed_session, previous_version=previous_version, phase=PHASE_AWAITING_HEALTH
        )
    engine.dispose()


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def test_main_lifespan_rolls_back_when_the_health_check_never_passes(
    settings: Settings,
) -> None:
    _seed_awaiting_health(settings, previous_version="1.0.0")
    runner = _RunnerSpy(returncode=0)
    reexec = _ReexecSpy()

    app = create_app(
        settings=settings,
        update_check_transport=_offline_update_check_transport(),
        self_update_subprocess_runner=runner,
        self_update_reexec_fn=reexec,
        self_update_health_check_fn=lambda: False,
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/system/self-update/status", headers=_auth_headers(client)
        )

    assert len(runner.calls) == 1
    command, _timeout = runner.calls[0]
    assert tuple(command) == ("pip", "install", "collapsarr==1.0.0")
    assert reexec.calls == 1

    assert response.status_code == 200
    body = response.json()
    assert body["in_progress"] is False
    assert body["phase"] == "rolled_back"
    assert body["previous_version"] == "1.0.0"


def test_main_lifespan_clears_the_guard_when_the_health_check_passes(
    settings: Settings,
) -> None:
    _seed_awaiting_health(settings, previous_version="1.0.0")
    runner = _RunnerSpy()
    reexec = _ReexecSpy()

    app = create_app(
        settings=settings,
        update_check_transport=_offline_update_check_transport(),
        self_update_subprocess_runner=runner,
        self_update_reexec_fn=reexec,
        self_update_health_check_fn=lambda: True,
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/system/self-update/status", headers=_auth_headers(client)
        )

    assert runner.calls == []
    assert reexec.calls == 0

    assert response.status_code == 200
    body = response.json()
    assert body["in_progress"] is False
    assert body["phase"] == "idle"


def test_main_lifespan_is_a_noop_with_no_self_update_in_progress(
    settings: Settings,
) -> None:
    """The overwhelming common case -- every ordinary boot -- must not be
    slowed down or otherwise touched by this gate."""

    def _exploding_health_check() -> bool:
        raise AssertionError("health_check_fn must not be called when phase is idle")

    app = create_app(
        settings=settings,
        update_check_transport=_offline_update_check_transport(),
        self_update_health_check_fn=_exploding_health_check,
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/system/self-update/status", headers=_auth_headers(client)
        )

    assert response.status_code == 200
    assert response.json()["phase"] == "idle"
