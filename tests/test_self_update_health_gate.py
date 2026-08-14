"""Tests for the post-re-exec health-check gate + pinned-reinstall rollback
(COL-234).

Most of this file exercises the bare service-layer ``session`` fixture (no
HTTP app, no FastAPI lifespan), mirroring ``tests/test_self_update_apply.py``'s
convention -- ``resolve_awaiting_health`` has no FastAPI dependency of its
own (see ``collapsarr.self_update.health_gate``'s module docstring for why
it is wired into the lifespan rather than exposed as its own endpoint).
Every OS-touching seam (``health_check_fn``, ``subprocess_runner``,
``reexec_fn``, ``sleep_fn``) is a fake/spy throughout -- no real subprocess
spawn, re-exec, or wall-clock sleep ever happens in this file. The final
section exercises the real ``collapsarr.main`` wiring end to end (an
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
from collapsarr.self_update.service import (
    begin_self_update,
    get_self_update_state,
    set_self_update_phase,
)
from collapsarr.settings.service import get_global_settings


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
