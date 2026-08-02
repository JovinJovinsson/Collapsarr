"""The health-check scheduler (COL-75).

A dedicated background scheduler that mirrors the threading idiom of
:class:`collapsarr.backup.scheduler.BackupScheduler` /
:class:`collapsarr.jobs.scheduler.JobScheduler` -- a daemon thread running a
plain sleep/wake loop, an injectable clock for testing, and idempotent
``start()``/``stop()`` lifecycle methods wired into the app lifespan behind the
same ``enable_scheduler`` flag.

Unlike the backup scheduler there is no on-disk "due-ness": **every registered
check runs on every tick**, on a fixed five-minute cadence, starting
immediately when the loop starts. Each tick collects the results of all checks
and hands them to :func:`collapsarr.health.service.reconcile_health_results`,
which persists them and fires a notification only on a pass<->fail transition.

Threads, not asyncio: matches the sibling schedulers' rationale -- the checks
may do blocking I/O (``shutil.which`` today; socket/disk/DB probes later) and
there is no external scheduler dependency in ``pyproject.toml``. One bad check
is isolated (its exception is logged and the tick continues with the others),
and one bad tick never kills the loop.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.config import Settings

from .context import HealthCheckContext
from .registry import HealthCheck
from .result import HealthCheckResult
from .service import reconcile_health_results

logger = logging.getLogger(__name__)

#: Fixed cadence between ticks. The loop takes the first tick immediately on
#: start, then sleeps this long between subsequent ticks (woken early by stop).
INTERVAL_SECONDS = 300.0

_STOP_JOIN_TIMEOUT = 5.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HealthCheckScheduler:
    """Run every registered check on a fixed cadence, reconciling each tick.

    ``settings`` and short-lived sessions from ``session_factory`` are bundled
    into the :class:`~collapsarr.health.context.HealthCheckContext` handed to
    each check. ``checks`` is the registry (see
    :func:`collapsarr.health.registry.default_health_checks`).

    ``now`` is an injectable clock (defaults to real UTC now); tests pass a
    fixed one to assert ``first_failed_at`` / transition behaviour without real
    wall-clock time. ``transport`` is forwarded to the notifier dispatch (tests
    inject an ``httpx.MockTransport``). ``interval_seconds`` is overridable for
    tests but defaults to the fixed five-minute cadence.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        checks: Sequence[HealthCheck],
        *,
        now: Callable[[], datetime] = _utcnow,
        transport: httpx.BaseTransport | None = None,
        interval_seconds: float = INTERVAL_SECONDS,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._checks = list(checks)
        self._now = now
        self._transport = transport
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> list[HealthCheckResult]:
        """Run every check once, reconcile the results, and return them.

        This is the unit the loop calls each iteration and the seam the
        injectable-clock tests drive directly. A check that raises is logged and
        skipped so the rest of the tick (and its persistence/notifications)
        still happens.
        """
        results: list[HealthCheckResult] = []
        with self._session_factory() as session:
            context = HealthCheckContext(settings=self._settings, session=session)
            for check in self._checks:
                try:
                    results.extend(check.run(context))
                except Exception:  # noqa: BLE001 - one bad check must not skip the others
                    logger.exception("Health check %r raised; skipping it this tick", check.name)
            reconcile_health_results(
                session, results, now=self._now, transport=self._transport
            )
        return results

    def start(self) -> None:
        """Start the background loop in a daemon thread.

        Runs a tick immediately (so ``/health`` is populated and any pass->fail
        notification fires right away), then sleeps one interval between ticks.
        Idempotency is the caller's responsibility -- calling this twice raises.
        """
        if self._thread is not None:
            raise RuntimeError("HealthCheckScheduler is already started")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="collapsarr-health-scheduler", daemon=True
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
        """Sleep/wake loop: run a tick, then sleep one interval until the next."""
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
                logger.exception("health check tick failed")
            if self._stop.is_set():
                break
            self._stop.wait(timeout=self._interval_seconds)


__all__ = ["INTERVAL_SECONDS", "HealthCheckScheduler"]
