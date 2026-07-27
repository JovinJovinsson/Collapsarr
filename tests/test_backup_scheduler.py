"""Tests for the scheduled automatic backup loop (COL-67).

Follows the :class:`~collapsarr.jobs.scheduler.JobScheduler` test idiom: an
injectable clock (``now``) and real threading for the lifecycle test. Due-ness
is asserted against real archives on disk under the isolated ``settings``
fixture (a throwaway SQLite file + ``data_dir`` under ``tmp_path``), whose
modification times are backdated with :func:`os.utime` to model archives of a
controlled age -- exactly the on-disk timestamp the scheduler reads instead of
an in-memory timer.
"""

from __future__ import annotations

import os
import threading
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.backup.scheduler import BackupScheduler
from collapsarr.backup.service import (
    BACKUP_SCHEDULED,
    backup_type_dir,
    list_backups,
)
from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.migrations import upgrade_to_head

# Default interval is backup_interval_days == 7 (COL-66).
_FIXED_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(settings: Settings) -> Iterator[sessionmaker[Session]]:
    """A schema-initialised session factory over the isolated ``settings`` DB.

    Bringing the schema to head also creates a real, snapshot-able SQLite file,
    so :func:`~collapsarr.backup.service.is_backup_supported` is ``True`` and
    ``create_backup`` has something to ``VACUUM INTO``.
    """
    upgrade_to_head(settings)
    engine = create_engine_from_settings(settings)
    yield create_session_factory(engine)
    engine.dispose()


def _make_scheduler(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    now: datetime = _FIXED_NOW,
) -> BackupScheduler:
    return BackupScheduler(settings, session_factory, now=lambda: now)


def _scheduled_dir(settings: Settings) -> Path:
    return backup_type_dir(settings, BACKUP_SCHEDULED)


def _seed_scheduled_backup(settings: Settings, *, age: timedelta, base: datetime) -> Path:
    """Write a dummy ``scheduled/`` archive whose mtime is ``base - age``.

    The archive only needs to match the backup-filename glob and be a real file
    -- the scheduler's due-ness looks only at its modification time, never its
    contents. Backdating the mtime models an archive of a specific age relative
    to the scheduler's injected clock.
    """
    directory = _scheduled_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (base - age).strftime("%Y.%m.%d_%H.%M.%S")
    path = directory / f"collapsarr_backup_v0.0.0_{stamp}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("collapsarr.db", b"")
    when = (base - age).timestamp()
    os.utime(path, (when, when))
    return path


def _scheduled_archives(settings: Settings) -> list[Path]:
    return sorted(_scheduled_dir(settings).glob("collapsarr_backup_v*.zip"))


# ---------------------------------------------------------------------------
# Due-ness: fires when stale, skips when recent.
# ---------------------------------------------------------------------------


def test_run_once_takes_a_backup_when_no_scheduled_backup_exists(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """First boot: with no scheduled archive yet, one is taken immediately."""
    scheduler = _make_scheduler(settings, session_factory)

    info = scheduler.run_once()

    assert info is not None
    assert info.type == BACKUP_SCHEDULED
    assert len(_scheduled_archives(settings)) == 1


def test_run_once_takes_a_backup_when_newest_is_older_than_the_interval(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    # Default interval is 7 days; an 8-day-old archive is stale -> due.
    _seed_scheduled_backup(settings, age=timedelta(days=8), base=_FIXED_NOW)
    scheduler = _make_scheduler(settings, session_factory)

    assert scheduler.is_due() is True
    info = scheduler.run_once()

    assert info is not None
    assert info.type == BACKUP_SCHEDULED
    # The freshly-taken archive is now on disk alongside the stale seed.
    assert len(_scheduled_archives(settings)) == 2


def test_run_once_skips_when_a_recent_scheduled_backup_exists(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    # A 1-day-old archive is well within the 7-day interval -> not due.
    _seed_scheduled_backup(settings, age=timedelta(days=1), base=_FIXED_NOW)
    scheduler = _make_scheduler(settings, session_factory)

    assert scheduler.is_due() is False
    assert scheduler.run_once() is None
    # No new archive was written.
    assert len(_scheduled_archives(settings)) == 1


def test_due_ness_uses_the_live_interval_setting(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """Changing ``backup_interval_days`` is honoured on the next check."""
    from collapsarr.settings.service import update_global_settings

    _seed_scheduled_backup(settings, age=timedelta(days=3), base=_FIXED_NOW)
    scheduler = _make_scheduler(settings, session_factory)

    # 3-day-old archive under the default 7-day interval: not due.
    assert scheduler.is_due() is False

    # Tighten the interval to 1 day; the same 3-day-old archive is now stale.
    with session_factory() as session:
        update_global_settings(session, backup_interval_days=1)

    assert scheduler.is_due() is True


# ---------------------------------------------------------------------------
# Restart-safety: due-ness is on-disk, so a restart never double-fires.
# ---------------------------------------------------------------------------


def test_restart_does_not_double_fire(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A fresh scheduler right after a backup reads the same on-disk clock -> no re-fire."""
    first = _make_scheduler(settings, session_factory)
    info = first.run_once()
    assert info is not None
    assert len(_scheduled_archives(settings)) == 1

    # Simulate a restart: a brand-new scheduler instance (nothing carried over
    # in memory), clock advanced only slightly -- well inside the interval.
    restarted = _make_scheduler(
        settings, session_factory, now=_FIXED_NOW + timedelta(minutes=5)
    )

    assert restarted.is_due() is False
    assert restarted.run_once() is None
    # Still exactly one archive: the restart did not take a second backup.
    assert len(_scheduled_archives(settings)) == 1


# ---------------------------------------------------------------------------
# Catch-up: a window missed while the app was down is taken on next boot.
# ---------------------------------------------------------------------------


def test_missed_window_is_caught_up_on_boot(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """An archive older than the interval (app was down) is backed up immediately on boot."""
    # The last scheduled backup is 10 days old; interval is 7 -> a window was
    # missed while the process was down. On boot the scheduler should catch up.
    _seed_scheduled_backup(settings, age=timedelta(days=10), base=_FIXED_NOW)
    scheduler = _make_scheduler(settings, session_factory)

    assert scheduler.is_due() is True
    info = scheduler.run_once()

    assert info is not None
    assert len(_scheduled_archives(settings)) == 2


# ---------------------------------------------------------------------------
# Non-file / :memory: database: no scheduler activity.
# ---------------------------------------------------------------------------


def test_run_once_no_ops_for_a_memory_database(tmp_path: Path) -> None:
    """An in-memory database can't be snapshotted -> the scheduler does nothing."""
    settings = Settings(
        data_dir=str(tmp_path), database_url="sqlite+pysqlite:///:memory:"
    )
    # No session factory is needed: the support check short-circuits before any
    # interval read. A factory that would raise if used proves it isn't touched.
    def _exploding_factory() -> Session:  # pragma: no cover - must never run
        raise AssertionError("session factory must not be used for a :memory: database")

    scheduler = BackupScheduler(settings, _exploding_factory, now=lambda: _FIXED_NOW)  # type: ignore[arg-type]

    assert scheduler.run_once() is None
    assert list_backups(settings) == []


# ---------------------------------------------------------------------------
# Background loop: real threading, daemon thread, clean start/stop.
# ---------------------------------------------------------------------------


def test_start_takes_the_first_backup_then_stops_cleanly(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """The daemon loop takes the (due) first backup, and stop() joins cleanly."""
    scheduler = _make_scheduler(settings, session_factory)

    scheduler.start()
    try:
        assert scheduler._thread is not None
        assert scheduler._thread.daemon is True
        _wait_until(lambda: len(_scheduled_archives(settings)) == 1, timeout=5.0)
    finally:
        scheduler.stop()

    assert scheduler._thread is None
    assert len(_scheduled_archives(settings)) == 1


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


def test_start_does_not_backup_when_a_recent_one_exists(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A recent archive means the started loop leaves the count untouched."""
    _seed_scheduled_backup(settings, age=timedelta(days=1), base=datetime.now(UTC))
    scheduler = BackupScheduler(settings, session_factory)  # real clock

    scheduler.start()
    try:
        # Give the loop a moment to run its first iteration.
        _wait_until(lambda: scheduler._thread is not None, timeout=1.0)
    finally:
        scheduler.stop()

    assert len(_scheduled_archives(settings)) == 1


def _wait_until(predicate: object, *, timeout: float) -> None:
    import time

    deadline = time.monotonic() + timeout
    assert callable(predicate)
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def test_daemon_thread_naming(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """Sanity: the loop runs on a named daemon thread (JobScheduler idiom)."""
    scheduler = _make_scheduler(settings, session_factory)
    scheduler.start()
    try:
        thread = scheduler._thread
        assert isinstance(thread, threading.Thread)
        assert thread.name == "collapsarr-backup-scheduler"
        assert thread.daemon is True
    finally:
        scheduler.stop()
