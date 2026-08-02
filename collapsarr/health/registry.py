"""Health-check registration (COL-75).

A :class:`HealthCheck` pairs a stable ``name`` (for logging / the scheduler's
thread) with a ``run`` callable that takes a
:class:`~collapsarr.health.context.HealthCheckContext` and returns the current
:class:`~collapsarr.health.result.HealthCheckResult` values for the Check
Key(s) it owns. A check may return several results (e.g. a per-instance check
returns one per Arr instance).

:func:`default_health_checks` is the single place the app assembles the
registry. Each later ticket in Epic COL-74 (no-instances, Arr-unreachable, disk
space, database-writable, failed-jobs) adds its check here; COL-75 shipped the
migrated FFmpeg presence check and COL-77 adds the no-Arr-instances-configured
check.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .arr_instances import ARR_INSTANCES_CHECK_NAME, run_arr_instances_check
from .context import HealthCheckContext
from .ffmpeg import (
    FFMPEG_CHECK_NAME,
    FfmpegCheckResult,
    check_ffmpeg,
    make_ffmpeg_check_run,
)
from .result import HealthCheckResult


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """A registered health check: a name plus the callable that runs it."""

    name: str
    run: Callable[[HealthCheckContext], Sequence[HealthCheckResult]]


def default_health_checks(
    ffmpeg_checker: Callable[[], FfmpegCheckResult] | None = None,
) -> list[HealthCheck]:
    """Build the registry of checks the scheduler runs each tick.

    ``ffmpeg_checker`` overrides the FFmpeg presence probe (defaults to
    :func:`~collapsarr.health.ffmpeg.check_ffmpeg`), letting the app/tests
    simulate a present/missing FFmpeg without touching the real binary.
    """
    return [
        HealthCheck(
            name=FFMPEG_CHECK_NAME,
            run=make_ffmpeg_check_run(ffmpeg_checker or check_ffmpeg),
        ),
        HealthCheck(
            name=ARR_INSTANCES_CHECK_NAME,
            run=run_arr_instances_check,
        ),
    ]
