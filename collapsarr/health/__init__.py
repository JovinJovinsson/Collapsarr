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
- **The no-Arr-instances check** -- :func:`run_arr_instances_check`
  (:mod:`~collapsarr.health.arr_instances`, COL-77): warns when zero Sonarr/
  Radarr instances are configured.
- **The Arr-unreachable check** -- :func:`make_arr_connectivity_check_run`
  (:mod:`~collapsarr.health.arr_connectivity`, COL-78): a live, per-instance
  reachability probe against every configured Arr instance.
- **The disk-space check** -- :func:`make_disk_space_check_run`
  (:mod:`~collapsarr.health.disk_space`, COL-79): a two-tier (warning/error)
  free-space-percentage check against the data directory's filesystem, with
  live-configurable thresholds.
- **The database-unwritable check** -- :func:`run_database_writable_check`
  (:mod:`~collapsarr.health.database_writable`, COL-80): a live
  write-and-commit probe against the dedicated :class:`HealthWriteProbe`
  table, catching a database that has become unwritable (locking,
  permissions, a read-only mount) regardless of the configured backend.
"""

from __future__ import annotations

from .arr_connectivity import (
    ARR_CONNECTIVITY_CATEGORY,
    ARR_CONNECTIVITY_CHECK_NAME,
    ARR_UNREACHABLE_CODE,
    make_arr_connectivity_check_run,
)
from .arr_instances import (
    ARR_INSTANCES_CATEGORY,
    ARR_INSTANCES_CHECK_NAME,
    NO_ARR_INSTANCES_CODE,
    run_arr_instances_check,
)
from .context import HealthCheckContext
from .database_writable import (
    DATABASE_UNWRITABLE_CODE,
    DATABASE_WRITABLE_CATEGORY,
    DATABASE_WRITABLE_CHECK_NAME,
    run_database_writable_check,
)
from .disk_space import (
    DISK_SPACE_CATEGORY,
    DISK_SPACE_CHECK_NAME,
    DISK_SPACE_ERROR_CODE,
    DISK_SPACE_WARNING_CODE,
    DiskUsage,
    free_space_percent,
    make_disk_space_check_run,
)
from .ffmpeg import (
    FFMPEG_CATEGORY,
    FFMPEG_CHECK_NAME,
    FFMPEG_MISSING_CODE,
    FfmpegCheckResult,
    check_ffmpeg,
    make_ffmpeg_check_run,
)
from .models import (
    CHECK_STATUS_FAILING,
    CHECK_STATUS_PASSING,
    WRITE_PROBE_ID,
    HealthCheckState,
    HealthWriteProbe,
)
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
    "ARR_CONNECTIVITY_CATEGORY",
    "ARR_CONNECTIVITY_CHECK_NAME",
    "ARR_INSTANCES_CATEGORY",
    "ARR_INSTANCES_CHECK_NAME",
    "ARR_UNREACHABLE_CODE",
    "CHECK_STATUS_FAILING",
    "CHECK_STATUS_PASSING",
    "DATABASE_UNWRITABLE_CODE",
    "DATABASE_WRITABLE_CATEGORY",
    "DATABASE_WRITABLE_CHECK_NAME",
    "DISK_SPACE_CATEGORY",
    "DISK_SPACE_CHECK_NAME",
    "DISK_SPACE_ERROR_CODE",
    "DISK_SPACE_WARNING_CODE",
    "EVENT_HEALTH_CHECK_FAILED",
    "EVENT_HEALTH_CHECK_RECOVERED",
    "FFMPEG_CATEGORY",
    "FFMPEG_CHECK_NAME",
    "FFMPEG_MISSING_CODE",
    "INTERVAL_SECONDS",
    "NO_ARR_INSTANCES_CODE",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "WRITE_PROBE_ID",
    "DiskUsage",
    "FfmpegCheckResult",
    "HealthCheck",
    "HealthCheckContext",
    "HealthCheckResult",
    "HealthCheckScheduler",
    "HealthCheckState",
    "HealthWriteProbe",
    "Severity",
    "check_ffmpeg",
    "default_health_checks",
    "free_space_percent",
    "list_failing_checks",
    "list_health_check_states",
    "make_arr_connectivity_check_run",
    "make_disk_space_check_run",
    "make_ffmpeg_check_run",
    "reconcile_health_results",
    "run_arr_instances_check",
    "run_database_writable_check",
]
