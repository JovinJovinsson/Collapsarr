"""Take database backups automatically on the configured interval (COL-67).

A dedicated background scheduler that mirrors the threading idiom of
:class:`collapsarr.jobs.scheduler.JobScheduler` -- a daemon thread running a
plain sleep/wake loop, an injectable clock for testing, and idempotent
``start()``/``stop()`` lifecycle methods wired into the app lifespan behind the
same ``enable_scheduler`` flag. It writes ``scheduled`` backups into
``<data_dir>/backups/scheduled/`` via the shared
:func:`collapsarr.backup.service.create_backup` service.

Due-ness is on-disk, not an in-memory timer
--------------------------------------------
The single most important design property: **whether a scheduled backup is due
is computed from the modification time of the newest archive already sitting in
``backups/scheduled/``**, never from a timer started when the process booted.
The consequences, matching Radarr's scheduled-backup behaviour:

- **Restart-safe (no double-fire):** a fresh process reads the same on-disk
  timestamp the previous one wrote, so restarting the app moments after a
  backup does *not* trigger another -- the clock lives on disk, not in the
  process.
- **Catch-up on boot:** if the app was down across a due window, the newest
  scheduled archive is already older than the interval when the app comes back
  up, so the first loop iteration takes the missed backup immediately rather
  than waiting a fresh full interval.

The interval itself is read live from the ``global_settings`` row's
``backup_interval_days`` (COL-66) on every check, so an operator changing it via
the Settings API is honoured on the next cycle without a restart.

No-op on non-file databases
---------------------------
When the database isn't a file-based SQLite one -- a non-SQLite
``database_url`` override or the ``:memory:`` sentinel --
:func:`collapsarr.backup.service.is_backup_supported` is ``False`` and the loop
does nothing (no snapshot is meaningful there), exactly as the manual
"Backup Now" path already no-ops.

Threads, not asyncio: matches :mod:`collapsarr.jobs.scheduler`'s rationale --
``VACUUM INTO`` is a blocking SQLite call and there is no external scheduler
dependency in ``pyproject.toml``. The loop is a plain sleep/wake ``threading``
loop: it takes a backup if one is due, then sleeps until the next archive would
fall due (woken early only by :meth:`stop`).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from collapsarr.config import Settings
from collapsarr.settings.service import get_global_settings

from .service import (
    BACKUP_SCHEDULED,
    BackupInfo,
    BackupUnavailableError,
    create_backup,
    is_backup_supported,
    list_backups,
)

logger = logging.getLogger(__name__)

#: How long a loop iteration sleeps at minimum before re-checking. Floors the
#: computed "seconds until next due" so a transient failure (e.g. a backup that
#: raised) is retried on a sane cadence rather than in a tight busy-loop, and so
#: an unsupported database is re-checked periodically without spinning.
_MIN_SLEEP_SECONDS = 60.0

_STOP_JOIN_TIMEOUT = 5.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BackupScheduler:
    """Take a ``scheduled`` backup whenever the newest on-disk one is stale.

    ``settings`` supplies the ``data_dir`` (where ``backups/scheduled/`` lives)
    and the database configuration :func:`is_backup_supported` inspects.
    ``session_factory`` opens short-lived sessions to read the live
    ``backup_interval_days`` from the ``global_settings`` row.

    ``now`` is an injectable clock (defaults to real UTC now); tests pass a
    fixed/controllable one to exercise due-ness, restart-safety, and catch-up
    without waiting real wall-clock time.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._now = now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- Due-ness (computed from disk, not an in-memory timer) ---------------

    def _interval(self) -> timedelta:
        """Read the live backup interval from ``global_settings`` (COL-66)."""
        with self._session_factory() as session:
            days = get_global_settings(session).backup_interval_days
        return timedelta(days=days)

    def _retention_days(self) -> int:
        """Read the live backup retention window from ``global_settings`` (COL-66).

        Passed to :func:`~collapsarr.backup.service.create_backup` so the
        post-backup prune (COL-68) deletes ``scheduled`` archives past the
        configured window. Read live on each backup so an operator's Settings
        change is honoured without a restart.
        """
        with self._session_factory() as session:
            return get_global_settings(session).backup_retention_days

    def _newest_scheduled_at(self) -> datetime | None:
        """Modification time (UTC) of the newest ``scheduled/`` archive, or ``None``.

        Derived from the on-disk archives via
        :func:`~collapsarr.backup.service.list_backups`, so it survives a
        process restart -- the timestamp is the file's, not the process's.
        """
        scheduled = [
            info for info in list_backups(self._settings) if info.type == BACKUP_SCHEDULED
        ]
        if not scheduled:
            return None
        return max(info.created_at for info in scheduled)

    def is_due(self) -> bool:
        """Whether a scheduled backup should be taken right now.

        Due when there is no ``scheduled/`` archive yet (first boot, so a
        recovery point is established immediately) or the newest one is at least
        one interval old (the normal cadence, and the catch-up case after the
        app was down across a window).
        """
        newest = self._newest_scheduled_at()
        if newest is None:
            return True
        return self._now() - newest >= self._interval()

    def run_once(self) -> BackupInfo | None:
        """Take a scheduled backup if one is due; return it, else ``None``.

        No-ops (returning ``None``) when the database isn't a file-based SQLite
        one or when a recent enough scheduled backup already exists. This is the
        unit the loop calls each iteration and the seam the injectable-clock
        tests drive directly.
        """
        if not is_backup_supported(self._settings):
            return None
        if not self.is_due():
            return None
        try:
            info = create_backup(
                self._settings,
                BACKUP_SCHEDULED,
                retention_days=self._retention_days(),
                now=self._now(),
            )
        except BackupUnavailableError:
            # The database stopped being file-based between the support check
            # and the snapshot (a configuration race). Treat as a no-op.
            logger.warning("scheduled backup skipped: database no longer supports backups")
            return None
        logger.info("Scheduled backup written: %s", info.id)
        return info

    def _seconds_until_next_due(self) -> float:
        """Seconds to sleep before the next due check, floored at :data:`_MIN_SLEEP_SECONDS`.

        Computed from the newest on-disk scheduled archive plus the interval, so
        the loop wakes right around when the next backup falls due. An
        unsupported database has nothing to schedule, so it re-checks once per
        interval (config could change) rather than spinning.
        """
        interval = self._interval()
        if not is_backup_supported(self._settings):
            return max(_MIN_SLEEP_SECONDS, interval.total_seconds())
        newest = self._newest_scheduled_at()
        if newest is None:
            remaining = 0.0
        else:
            remaining = (newest + interval - self._now()).total_seconds()
        return max(_MIN_SLEEP_SECONDS, remaining)

    # -- Background loop lifecycle ------------------------------------------

    def start(self) -> None:
        """Start the background backup loop in a daemon thread.

        Runs a due check immediately (taking the first/missed backup right
        away), then sleeps until the next archive would fall due. Idempotency is
        the caller's responsibility -- calling this twice raises.
        """
        if self._thread is not None:
            raise RuntimeError("BackupScheduler is already started")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="collapsarr-backup-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float | None = _STOP_JOIN_TIMEOUT) -> None:
        """Signal the loop to stop and join its thread (a no-op if not started)."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        """Sleep/wake loop: take a backup when due, then sleep until the next one."""
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - one bad backup must not kill the loop
                logger.exception("scheduled backup check failed")
            if self._stop.is_set():
                break
            self._stop.wait(timeout=self._seconds_until_next_due())


__all__ = ["BackupScheduler"]
