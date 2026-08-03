"""The context handed to every health check when it runs (COL-75).

A small bundle of the two things a check might need to probe its concern: the
application :class:`~collapsarr.config.Settings` (data dir, database path, disk
paths) and an open :class:`~sqlalchemy.orm.Session` (Arr instances, job
history). Passed positionally to each check by
:meth:`collapsarr.health.scheduler.HealthCheckScheduler.run_once`; the FFmpeg
check ignores both, later per-instance/DB checks use them.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from collapsarr.config import Settings


@dataclass(frozen=True, slots=True)
class HealthCheckContext:
    """Ambient dependencies available to a health check during a tick."""

    settings: Settings
    session: Session
