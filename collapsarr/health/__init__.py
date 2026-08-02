"""Health Check Framework (Epic COL-74, core in COL-75).

A pluggable framework that generalises the app's original one-shot FFmpeg
startup check into a set of registered checks run by a background scheduler on a
fixed cadence, diffed against persisted per-check state so a notification fires
only on a pass<->fail transition, and surfaced on the unauthenticated
``/health`` liveness endpoint.

Public surface, by concern:

- **Result shape** -- :class:`HealthCheckResult` + the ``SEVERITY_*`` vocabulary
  (:mod:`~collapsarr.health.result`): the one datatype checks speak in.
- **Registration** -- :class:`HealthCheck` + :func:`default_health_checks`
  (:mod:`~collapsarr.health.registry`) and the per-check
  :class:`HealthCheckContext` (:mod:`~collapsarr.health.context`).
- **Persistence** -- :class:`HealthCheckState`
  (:mod:`~collapsarr.health.models`); imported here so the model registers with
  :data:`collapsarr.database.Base.metadata`.
- **Orchestration** -- :class:`HealthCheckScheduler`
  (:mod:`~collapsarr.health.scheduler`) and the reconcile/read service
  (:mod:`~collapsarr.health.service`).
- **The FFmpeg check** -- :func:`check_ffmpeg` + :class:`FfmpegCheckResult`
  (:mod:`~collapsarr.health.ffmpeg`), the probe migrated verbatim from COL-38.
"""

from __future__ import annotations

from .context import HealthCheckContext
from .ffmpeg import (
    FFMPEG_CATEGORY,
    FFMPEG_CHECK_NAME,
    FFMPEG_MISSING_CODE,
    FfmpegCheckResult,
    check_ffmpeg,
    make_ffmpeg_check_run,
)
from .models import CHECK_STATUS_FAILING, CHECK_STATUS_PASSING, HealthCheckState
from .registry import HealthCheck, default_health_checks
from .result import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    HealthCheckResult,
    Severity,
)
from .scheduler import INTERVAL_SECONDS, HealthCheckScheduler
from .service import (
    EVENT_HEALTH_CHECK_FAILED,
    EVENT_HEALTH_CHECK_RECOVERED,
    list_failing_checks,
    list_health_check_states,
    reconcile_health_results,
)

__all__ = [
    "CHECK_STATUS_FAILING",
    "CHECK_STATUS_PASSING",
    "EVENT_HEALTH_CHECK_FAILED",
    "EVENT_HEALTH_CHECK_RECOVERED",
    "FFMPEG_CATEGORY",
    "FFMPEG_CHECK_NAME",
    "FFMPEG_MISSING_CODE",
    "INTERVAL_SECONDS",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "FfmpegCheckResult",
    "HealthCheck",
    "HealthCheckContext",
    "HealthCheckResult",
    "HealthCheckScheduler",
    "HealthCheckState",
    "Severity",
    "check_ffmpeg",
    "default_health_checks",
    "list_failing_checks",
    "list_health_check_states",
    "make_ffmpeg_check_run",
    "reconcile_health_results",
]
