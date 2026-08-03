"""Tests for the Update Check scheduler orchestration (COL-86).

Follows :mod:`tests.test_health_scheduler`'s idiom: an injectable clock
(``now``) so ``checked_at`` is asserted without real wall-clock time,
``run_once`` driven directly as the seam (no real sleeping, ``httpx.
MockTransport`` standing in for the GitHub fetch), and real threading only
for the lifecycle test.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.migrations import upgrade_to_head
from collapsarr.settings.models import UPDATE_CHANNEL_STABLE
from collapsarr.settings.service import get_global_settings
from collapsarr.update_check.scheduler import UpdateCheckScheduler
from collapsarr.update_check.service import get_update_check_state

_FIXED_NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(settings: Settings) -> Iterator[sessionmaker[Session]]:
    """A schema-initialised session factory over the isolated ``settings`` DB."""
    upgrade_to_head(settings)
    engine = create_engine_from_settings(settings)
    yield create_session_factory(engine)
    engine.dispose()


def _success_transport(tag: str = "v9.9.9") -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "tag_name": tag,
                "name": tag,
                "body": "changelog",
                "published_at": "2026-08-01T00:00:00Z",
            },
        )

    return httpx.MockTransport(handler), seen


def _failing_transport() -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    return httpx.MockTransport(handler)


def _make_scheduler(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    now: datetime = _FIXED_NOW,
    transport: httpx.BaseTransport | None = None,
) -> UpdateCheckScheduler:
    if transport is None:
        transport, _ = _success_transport()
    return UpdateCheckScheduler(settings, session_factory, now=lambda: now, transport=transport)


def _state(session_factory: sessionmaker[Session]) -> object:
    with session_factory() as session:
        return get_update_check_state(session)


# ---------------------------------------------------------------------------
# Each tick fetches and persists the singleton state.
# ---------------------------------------------------------------------------


def test_run_once_fetches_and_persists_state(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    transport, seen = _success_transport("v1.2.3")
    scheduler = _make_scheduler(settings, session_factory, transport=transport)

    result = scheduler.run_once()

    assert len(seen) == 1
    assert result.latest_tag == "v1.2.3"
    with session_factory() as session:
        row = get_update_check_state(session)
        assert row is not None
        assert row.latest_tag == "v1.2.3"
        assert row.channel == UPDATE_CHANNEL_STABLE


def test_run_once_reads_the_configured_channel_fresh_each_tick(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    # Flip the configured channel via the settings service (creates the
    # singleton global_settings row on first access), then confirm the very
    # next tick records the new channel -- no restart required.
    with session_factory() as session:
        row = get_global_settings(session)
        row.update_channel = "beta"
        session.commit()

    scheduler = _make_scheduler(settings, session_factory)
    scheduler.run_once()

    with session_factory() as session:
        state = get_update_check_state(session)
        assert state is not None
        assert state.channel == "beta"


# ---------------------------------------------------------------------------
# Injectable clock: checked_at is stamped with it.
# ---------------------------------------------------------------------------


def test_checked_at_is_stamped_with_the_injected_clock(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, now=_FIXED_NOW)

    scheduler.run_once()

    # SQLite's DateTime column round-trips as a naive datetime, so compare
    # against the injected clock with its UTC tzinfo stripped.
    expected = _FIXED_NOW.replace(tzinfo=None)
    with session_factory() as session:
        row = get_update_check_state(session)
        assert row is not None
        assert row.checked_at == expected


# ---------------------------------------------------------------------------
# A failed fetch does not raise and does not clobber prior good data.
# ---------------------------------------------------------------------------


def test_a_failing_fetch_does_not_raise_and_preserves_prior_data(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    good_transport, _ = _success_transport("v2.0.0")
    first = _make_scheduler(settings, session_factory, now=_FIXED_NOW, transport=good_transport)
    first.run_once()

    later = _FIXED_NOW.replace(hour=13)
    failing = _make_scheduler(
        settings, session_factory, now=later, transport=_failing_transport()
    )
    result = failing.run_once()  # must not raise

    assert result.latest_tag == "v2.0.0"  # preserved, not blanked out
    assert result.checked_at == later  # still in-session; not round-tripped through SQLite


# ---------------------------------------------------------------------------
# Background loop: real threading, daemon thread, clean start/stop.
# ---------------------------------------------------------------------------


def test_start_runs_an_immediate_tick_then_stops_cleanly(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    transport, seen = _success_transport()
    scheduler = _make_scheduler(settings, session_factory, transport=transport)

    scheduler.start()
    try:
        assert scheduler._thread is not None
        assert scheduler._thread.daemon is True
        _wait_until(lambda: len(seen) >= 1, timeout=5.0)
    finally:
        scheduler.stop()

    assert scheduler._thread is None
    assert _state(session_factory) is not None


def test_start_twice_raises(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory)

    scheduler.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            scheduler.start()
    finally:
        scheduler.stop()


def test_stop_is_safe_before_start(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory)
    scheduler.stop()  # must not raise


def test_daemon_thread_naming(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory)
    scheduler.start()
    try:
        thread = scheduler._thread
        assert isinstance(thread, threading.Thread)
        assert thread.name == "collapsarr-update-check-scheduler"
        assert thread.daemon is True
    finally:
        scheduler.stop()


# ---------------------------------------------------------------------------
# One bad tick must not kill the background loop.
# ---------------------------------------------------------------------------


def test_a_raising_tick_does_not_kill_the_loop(
    settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = _make_scheduler(settings, session_factory)

    calls = {"n": 0}
    real_run_once = scheduler.run_once

    def flaky_run_once() -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated tick failure")
        return real_run_once()

    monkeypatch.setattr(scheduler, "run_once", flaky_run_once)
    scheduler._interval_seconds = 0.05

    scheduler.start()
    try:
        _wait_until(lambda: calls["n"] >= 2, timeout=5.0)
    finally:
        scheduler.stop()

    assert calls["n"] >= 2  # the loop survived the first tick's exception


def _wait_until(predicate: object, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    assert callable(predicate)
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")
