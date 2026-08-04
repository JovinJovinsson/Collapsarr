"""Tests for the health-check scheduler orchestration (COL-75).

Follows the :class:`~collapsarr.backup.scheduler.BackupScheduler` test idiom: an
injectable clock (``now``) so ``first_failed_at`` and transition behaviour are
asserted without real wall-clock time, ``run_once`` driven directly as the seam
(no real sleeping), and real threading only for the lifecycle test. A
controllable fake check toggles between passing/failing to exercise
due/transition/persistence/restart-safety.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.health import (
    CHECK_STATUS_FAILING,
    SEVERITY_ERROR,
    HealthCheck,
    HealthCheckContext,
    HealthCheckResult,
    HealthCheckScheduler,
    list_health_check_states,
)
from collapsarr.migrations import upgrade_to_head
from collapsarr.notify.service import update_notifier_config

_FIXED_NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
_TEST_CODE = "TEST-001"


@pytest.fixture
def session_factory(settings: Settings) -> Iterator[sessionmaker[Session]]:
    """A schema-initialised session factory over the isolated ``settings`` DB."""
    upgrade_to_head(settings)
    engine = create_engine_from_settings(settings)
    yield create_session_factory(engine)
    engine.dispose()


class _ToggleCheck:
    """A fake check whose pass/fail is flipped between ticks by the test."""

    def __init__(self, *, passing: bool) -> None:
        self.passing = passing
        self.calls = 0

    def run(self, _context: HealthCheckContext) -> list[HealthCheckResult]:
        self.calls += 1
        message = "up" if self.passing else "down"
        return [
            HealthCheckResult(
                code=_TEST_CODE,
                category="test",
                severity=SEVERITY_ERROR,
                message=message,
                passing=self.passing,
            )
        ]


def _capture_transport() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    return httpx.MockTransport(handler), seen


def _enable_notifier(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        update_notifier_config(
            session, webhook_url="https://example.com/hook", webhook_enabled=True
        )


def _make_scheduler(
    settings: Settings,
    session_factory: sessionmaker[Session],
    check: _ToggleCheck,
    *,
    now: datetime = _FIXED_NOW,
    transport: httpx.BaseTransport | None = None,
) -> HealthCheckScheduler:
    return HealthCheckScheduler(
        settings,
        session_factory,
        [HealthCheck(name="test", run=check.run)],
        now=lambda: now,
        transport=transport,
    )


def _failing_row(session_factory: sessionmaker[Session]) -> bool:
    with session_factory() as session:
        rows = list_health_check_states(session)
        return len(rows) == 1 and rows[0].status == CHECK_STATUS_FAILING


# ---------------------------------------------------------------------------
# Each tick runs every registered check.
# ---------------------------------------------------------------------------


def test_run_once_runs_the_check_and_persists_its_state(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    check = _ToggleCheck(passing=False)
    scheduler = _make_scheduler(settings, session_factory, check)

    results = scheduler.run_once()

    assert check.calls == 1
    assert len(results) == 1
    assert _failing_row(session_factory)


# ---------------------------------------------------------------------------
# Transitions: notify on pass<->fail, never for an unchanged still-failing check.
# ---------------------------------------------------------------------------


def test_notifies_once_on_failure_and_not_again_while_failing(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _enable_notifier(session_factory)
    transport, seen = _capture_transport()
    check = _ToggleCheck(passing=False)
    scheduler = _make_scheduler(settings, session_factory, check, transport=transport)

    scheduler.run_once()  # pass -> fail
    assert len(seen) == 1

    scheduler.run_once()  # still failing -> no new notification
    scheduler.run_once()
    assert len(seen) == 1


def test_notifies_on_recovery(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _enable_notifier(session_factory)
    transport, seen = _capture_transport()
    check = _ToggleCheck(passing=False)
    scheduler = _make_scheduler(settings, session_factory, check, transport=transport)

    scheduler.run_once()  # fail
    check.passing = True
    scheduler.run_once()  # fail -> pass

    assert len(seen) == 2


# ---------------------------------------------------------------------------
# Persistence + injectable clock: first_failed_at records the streak start.
# ---------------------------------------------------------------------------


def test_first_failed_at_is_stamped_with_the_injected_clock(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    check = _ToggleCheck(passing=False)
    scheduler = _make_scheduler(settings, session_factory, check, now=_FIXED_NOW)

    scheduler.run_once()

    # SQLite's DateTime column round-trips as a naive datetime, so compare
    # against the injected clock with its UTC tzinfo stripped.
    expected = _FIXED_NOW.replace(tzinfo=None)
    with session_factory() as session:
        row = list_health_check_states(session)[0]
        assert row.first_failed_at == expected
        assert row.last_checked_at == expected


# ---------------------------------------------------------------------------
# Restart-safety: a still-failing check does not re-fire after a restart.
# ---------------------------------------------------------------------------


def test_restart_does_not_refire_a_still_failing_check(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _enable_notifier(session_factory)
    transport, seen = _capture_transport()

    check = _ToggleCheck(passing=False)
    first = _make_scheduler(settings, session_factory, check, now=_FIXED_NOW, transport=transport)
    first.run_once()  # pass -> fail: 1 notification, state persisted as failing
    assert len(seen) == 1
    first_failed_at = None
    with session_factory() as session:
        first_failed_at = list_health_check_states(session)[0].first_failed_at

    # Simulate a restart: a brand-new scheduler + check (nothing carried in
    # memory), clock advanced, check still failing. It reads the persisted
    # `failing` row and must NOT re-fire a "just failed" notification.
    restarted_check = _ToggleCheck(passing=False)
    restarted = _make_scheduler(
        settings,
        session_factory,
        restarted_check,
        now=_FIXED_NOW + timedelta(minutes=10),
        transport=transport,
    )
    restarted.run_once()

    assert len(seen) == 1
    with session_factory() as session:
        # Streak start is preserved across the restart (not reset to the new now).
        assert list_health_check_states(session)[0].first_failed_at == first_failed_at


# ---------------------------------------------------------------------------
# One bad check must not skip the others in the same tick.
# ---------------------------------------------------------------------------


def test_a_raising_check_does_not_prevent_the_others(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    def boom(_context: HealthCheckContext) -> list[HealthCheckResult]:
        raise RuntimeError("check exploded")

    good = _ToggleCheck(passing=False)
    scheduler = HealthCheckScheduler(
        settings,
        session_factory,
        [HealthCheck(name="boom", run=boom), HealthCheck(name="good", run=good.run)],
        now=lambda: _FIXED_NOW,
    )

    scheduler.run_once()  # must not raise

    assert good.calls == 1
    assert _failing_row(session_factory)


# ---------------------------------------------------------------------------
# Background loop: real threading, daemon thread, clean start/stop.
# ---------------------------------------------------------------------------


def test_start_runs_an_immediate_tick_then_stops_cleanly(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    check = _ToggleCheck(passing=False)
    scheduler = _make_scheduler(settings, session_factory, check)

    scheduler.start()
    try:
        assert scheduler._thread is not None
        assert scheduler._thread.daemon is True
        _wait_until(lambda: _failing_row(session_factory), timeout=5.0)
    finally:
        scheduler.stop()

    assert scheduler._thread is None
    assert _failing_row(session_factory)


def test_start_twice_raises(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, _ToggleCheck(passing=True))

    scheduler.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            scheduler.start()
    finally:
        scheduler.stop()


def test_stop_is_safe_before_start(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, _ToggleCheck(passing=True))
    scheduler.stop()  # must not raise


def test_daemon_thread_naming(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, _ToggleCheck(passing=True))
    scheduler.start()
    try:
        thread = scheduler._thread
        assert isinstance(thread, threading.Thread)
        assert thread.name == "collapsarr-health-scheduler"
        assert thread.daemon is True
    finally:
        scheduler.stop()


def _wait_until(predicate: object, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    assert callable(predicate)
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")
