"""The Update Check scheduler (COL-86, beta channel dispatch COL-88).

Structurally identical to :class:`collapsarr.health.scheduler.
HealthCheckScheduler`: a daemon thread running a plain sleep/wake loop, an
injectable clock for testing, and idempotent ``start()``/``stop()`` lifecycle
methods wired into the app lifespan behind the same ``enable_scheduler`` flag.
The one structural difference is cadence and cardinality -- there is a single
fixed check (fetch the latest release, reconcile one singleton row), not a
registry of pluggable checks -- so there is no ``checks: Sequence[...]``
parameter here.

Each tick fetches from whichever endpoint matches the currently configured
channel: :func:`~collapsarr.update_check.client.fetch_latest_release` for
``stable``, :func:`~collapsarr.update_check.client.fetch_latest_prerelease`
for ``beta`` (COL-88) -- selected fresh every tick from
:attr:`~collapsarr.settings.models.GlobalSettings.update_channel`, so an
operator switching channels takes effect on the very next tick.

Threads, not asyncio: matches every sibling scheduler's rationale -- the fetch
is blocking I/O (``httpx``) and there is no external scheduler dependency in
``pyproject.toml``. One bad tick (a raised exception anywhere in
:meth:`UpdateCheckScheduler.run_once`, e.g. a database error reconciling the
result) is logged and never kills the loop -- though
:func:`collapsarr.update_check.client.fetch_latest_release` itself already
never raises, so in practice a bad *fetch* just persists a "no result" tick
(see :mod:`collapsarr.update_check.service`) rather than reaching this
guard at all.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.config import Settings
from collapsarr.settings.models import UPDATE_CHANNEL_BETA
from collapsarr.settings.service import get_global_settings

from .client import fetch_latest_prerelease, fetch_latest_release
from .models import UpdateCheckState
from .service import reconcile_update_check

logger = logging.getLogger(__name__)

#: Fixed cadence between ticks. The loop takes the first tick immediately on
#: start, then sleeps this long between subsequent ticks (woken early by stop).
INTERVAL_SECONDS = 24 * 60 * 60.0

_STOP_JOIN_TIMEOUT = 5.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UpdateCheckScheduler:
    """Fetch the latest GitHub Release on a fixed 24h cadence, reconciling each tick.

    ``now`` is an injectable clock (defaults to real UTC now); tests pass a
    fixed one to assert ``checked_at`` without real wall-clock time.
    ``transport`` is forwarded to the GitHub client (tests inject an
    ``httpx.MockTransport``). ``interval_seconds`` is overridable for tests
    but defaults to the fixed 24-hour cadence.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        now: Callable[[], datetime] = _utcnow,
        transport: httpx.BaseTransport | None = None,
        interval_seconds: float = INTERVAL_SECONDS,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._now = now
        self._transport = transport
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> UpdateCheckState:
        """Fetch the latest release, reconcile it, and return the persisted state.

        This is the unit the loop calls each iteration and the seam the
        injectable-clock tests drive directly. Reads the configured channel
        from :class:`~collapsarr.settings.models.GlobalSettings` fresh every
        tick, so an operator changing it takes effect on the next tick with no
        restart -- and picks the matching fetch function (COL-88): the beta
        channel's latest prerelease
        (:func:`~collapsarr.update_check.client.fetch_latest_prerelease`)
        rather than the stable channel's latest release
        (:func:`~collapsarr.update_check.client.fetch_latest_release`).
        Neither fetch function ever raises; a failed fetch still reconciles
        (persisting a "no result" tick) -- see
        :mod:`collapsarr.update_check.service`.
        """
        with self._session_factory() as session:
            channel = get_global_settings(session).update_channel
            fetch = (
                fetch_latest_prerelease if channel == UPDATE_CHANNEL_BETA else fetch_latest_release
            )
            result = fetch(transport=self._transport)
            return reconcile_update_check(session, result, channel, now=self._now)

    def start(self, *, run_immediately: bool = True) -> None:
        """Start the background loop in a daemon thread.

        By default runs a tick immediately (so the cached release data is
        populated the instant the app comes up), then sleeps one interval
        between ticks. Pass ``run_immediately=False`` when the caller has
        already run the first tick synchronously (as the app lifespan does)
        and only wants the thread for the subsequent periodic ticks -- the
        loop then sleeps one interval before its first tick. Idempotency is
        the caller's responsibility -- calling this twice raises.
        """
        if self._thread is not None:
            raise RuntimeError("UpdateCheckScheduler is already started")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(run_immediately,),
            name="collapsarr-update-check-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float | None = _STOP_JOIN_TIMEOUT) -> None:
        """Signal the loop to stop and join its thread (a no-op if not started)."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self, run_immediately: bool = True) -> None:
        """Sleep/wake loop: run a tick, then sleep one interval until the next.

        When ``run_immediately`` is ``False`` the loop sleeps one interval
        before its first tick (the caller already ran the initial tick
        synchronously).
        """
        if not run_immediately:
            self._stop.wait(timeout=self._interval_seconds)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
                logger.exception("update check tick failed")
            if self._stop.is_set():
                break
            self._stop.wait(timeout=self._interval_seconds)


__all__ = ["INTERVAL_SECONDS", "UpdateCheckScheduler"]
