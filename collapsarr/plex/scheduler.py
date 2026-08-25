"""The Plex Sync scheduler (COL-210).

The fifth background scheduler, structurally a sibling of
:class:`collapsarr.update_check.scheduler.UpdateCheckScheduler` /
:class:`collapsarr.health.scheduler.HealthCheckScheduler` /
:class:`collapsarr.backup.scheduler.BackupScheduler` /
:class:`collapsarr.jobs.scheduler.JobScheduler`: a daemon thread running a plain
sleep/wake loop, an injectable clock for testing, a directly-driven single-tick
:meth:`run_once`, and idempotent ``start()``/``stop()`` lifecycle methods wired
into the app lifespan behind the same ``enable_scheduler`` flag.

Each tick rebuilds the Plex Library Item mapping table wholesale
(:func:`collapsarr.plex.library_sync.rebuild_library_items`) from the persisted
:class:`~collapsarr.plex.models.PlexConnection`, then -- reading that
freshly-rebuilt mapping -- refreshes every tracked, Plex-resolved file's
Default Audio Track display snapshot from Plex's current state
(:func:`collapsarr.plex.default_audio_snapshot.refresh_default_audio_snapshots`,
COL-248), so a default track changed directly in Plex's own UI (outside
Collapsarr) is picked up on the very next sync, with no Collapsarr Job
required. It runs:

- **weekly** -- the fixed :data:`INTERVAL_SECONDS` cadence (unlike the library
  scan's operator-tunable interval, a full Plex walk is heavy and its output
  only feeds the resolution *fallback*, so a fixed weekly refresh is enough);
- **immediately on Plex Connection save/reconnect** -- the ``PUT
  /api/plex/connection`` route calls :meth:`request_sync`, which wakes the loop
  to take an off-cycle tick *without blocking the settings-save response*
  (the sync happens on the background thread, not the request thread); and
- **on manual "Run now"** -- ``POST /api/plex/sync`` calls :meth:`run_once`
  directly.

Like the sibling schedulers, one bad tick is logged and never kills the loop,
and the Plex client calls are threaded through as injectable seams
(``list_sections``/``list_items``) so tests need no real Plex server.

:attr:`last_sync_at` is an in-memory marker (stamped at the *start* of every
:meth:`run_once`, mirroring :attr:`collapsarr.jobs.scheduler.JobScheduler.
last_scan_at`) that ``GET /api/system/tasks`` reads to compute this task's
next-run time; it resets on process restart, exactly like the library scan's.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from collapsarr.config import Settings

from .client import get_item_metadata, list_library_sections, list_section_items
from .default_audio_snapshot import GetMetadataFn, refresh_default_audio_snapshots
from .library_sync import ListItemsFn, ListSectionsFn, rebuild_library_items
from .service import get_plex_connection

logger = logging.getLogger(__name__)

#: Fixed weekly cadence between ticks. The loop takes the first tick immediately
#: on start, then sleeps this long between subsequent ticks (woken early by
#: :meth:`PlexSyncScheduler.stop` or :meth:`PlexSyncScheduler.request_sync`).
INTERVAL_SECONDS = 7 * 24 * 60 * 60.0

_STOP_JOIN_TIMEOUT = 5.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PlexSyncScheduler:
    """Rebuild the Plex Library Item mapping table weekly / on save / on demand.

    ``settings`` is currently unused by the tick itself (the connection is read
    from the database) but is accepted for symmetry with the sibling schedulers
    and future use. ``session_factory`` opens short-lived sessions to read the
    :class:`~collapsarr.plex.models.PlexConnection` and write the mapping table.

    ``now`` is an injectable clock (defaults to real UTC now); tests pass a
    fixed one to assert :attr:`last_sync_at` without real wall-clock time.
    ``transport`` is forwarded to the Plex client calls (tests inject an
    ``httpx.MockTransport``). ``list_sections``/``list_items``/``get_metadata``
    are the injectable client-call seams (defaulting to the real
    :mod:`collapsarr.plex.client` functions) -- ``get_metadata`` feeds the
    Default Audio Track snapshot refresh (COL-248) the same way
    ``list_sections``/``list_items`` feed the mapping-table rebuild -- so the
    sync can be driven with in-memory fakes. ``interval_seconds`` is
    overridable for tests but defaults to the fixed weekly cadence.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        now: Callable[[], datetime] = _utcnow,
        transport: object | None = None,
        list_sections: ListSectionsFn = list_library_sections,
        list_items: ListItemsFn = list_section_items,
        get_metadata: GetMetadataFn = get_item_metadata,
        interval_seconds: float = INTERVAL_SECONDS,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._now = now
        self._transport = transport
        self._list_sections = list_sections
        self._list_items = list_items
        self._get_metadata = get_metadata
        self._interval_seconds = interval_seconds
        self._last_sync_at: datetime | None = None
        self._stop = threading.Event()
        #: Set to break the loop's sleep early -- by :meth:`stop` (to exit) or
        #: :meth:`request_sync` (to take an off-cycle tick).
        self._wake = threading.Event()
        #: Set by :meth:`request_sync` to force the next wake to *run a tick*
        #: (not merely re-evaluate the timer), for the on-save/reconnect trigger.
        self._sync_requested = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def last_sync_at(self) -> datetime | None:
        """UTC timestamp the most recent :meth:`run_once` started, or ``None`` (COL-210).

        ``None`` until the first sync (weekly, on-save, or manual) runs -- an
        in-memory marker stamped at the top of :meth:`run_once`, so it resets on
        every process restart, exactly like
        :attr:`collapsarr.jobs.scheduler.JobScheduler.last_scan_at`. Exists so
        ``GET /api/system/tasks`` can compute this task's next-run time.
        """
        return self._last_sync_at

    def run_once(self) -> int:
        """Rebuild the mapping table, refresh the Default Audio Track snapshot, return its count.

        The unit the loop calls each iteration and the seam the tests drive
        directly. Stamps :attr:`last_sync_at` at the very start -- before any
        Plex I/O -- so it reflects when this pass began and is set even for a
        no-op tick (an unconfigured connection: :func:`~collapsarr.plex.
        library_sync.rebuild_library_items` no-ops on a blank ``base_url``,
        returning ``0`` and leaving any existing map untouched).

        After the mapping table is rebuilt, :func:`~collapsarr.plex.
        default_audio_snapshot.refresh_default_audio_snapshots` (COL-248) reads
        that freshly-rebuilt table and refreshes every tracked, Plex-resolved
        file's ``current_default_language``/``current_default_channel_layout``
        columns from Plex's current state, in the same session. The returned
        count is still the mapping table's row count (matching this method's
        existing contract, e.g. ``POST /api/plex/sync``'s response) -- the
        snapshot refresh's own count is not surfaced here.
        """
        self._last_sync_at = self._now()
        with self._session_factory() as session:
            connection = get_plex_connection(session)
            item_count = rebuild_library_items(
                session,
                base_url=connection.base_url,
                token=connection.token,
                transport=self._transport,
                list_sections=self._list_sections,
                list_items=self._list_items,
            )
            refresh_default_audio_snapshots(
                session,
                base_url=connection.base_url,
                token=connection.token,
                transport=self._transport,
                get_metadata=self._get_metadata,
            )
            return item_count

    def request_sync(self) -> None:
        """Ask the background loop to take an off-cycle sync as soon as possible (COL-210).

        Non-blocking: it only sets two events and returns, so the ``PUT
        /api/plex/connection`` route can trigger a sync on save/reconnect
        without blocking its HTTP response. The actual walk happens on the
        background thread's next wake. A no-op-in-effect when the loop isn't
        running (``enable_scheduler`` off): the flags are simply cleared by the
        next :meth:`start`.
        """
        self._sync_requested.set()
        self._wake.set()

    def start(self, *, run_immediately: bool = True) -> None:
        """Start the background loop in a daemon thread.

        By default runs a tick immediately (so the mapping table is warmed on
        startup), then sleeps one interval between ticks. Pass
        ``run_immediately=False`` to skip that first immediate tick. Idempotency
        is the caller's responsibility -- calling this twice raises.
        """
        if self._thread is not None:
            raise RuntimeError("PlexSyncScheduler is already started")
        self._stop.clear()
        self._wake.clear()
        self._sync_requested.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(run_immediately,),
            name="collapsarr-plex-sync-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float | None = _STOP_JOIN_TIMEOUT) -> None:
        """Signal the loop to stop and join its thread (a no-op if not started)."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self, run_immediately: bool = True) -> None:
        """Sleep/wake loop: run a tick when due (or when asked), then sleep until the next.

        Wakes early either to stop (:meth:`stop`) or to take an off-cycle,
        on-save/reconnect sync (:meth:`request_sync`, which sets
        :attr:`_sync_requested`). ``_sync_requested`` distinguishes "run now"
        from a spurious wake, so a stop wake doesn't accidentally trigger a tick.
        """
        next_sync = time.monotonic()
        if not run_immediately:
            next_sync += self._interval_seconds
        while not self._stop.is_set():
            if self._sync_requested.is_set() or time.monotonic() >= next_sync:
                self._sync_requested.clear()
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
                    logger.exception("plex sync tick failed")
                next_sync = time.monotonic() + self._interval_seconds
            if self._stop.is_set():
                break
            self._wake.wait(timeout=max(0.0, next_sync - time.monotonic()))
            self._wake.clear()


__all__ = ["INTERVAL_SECONDS", "PlexSyncScheduler"]
