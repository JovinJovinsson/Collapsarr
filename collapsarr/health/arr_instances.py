"""No-Arr-instances-configured health check (COL-77).

A fresh install with zero Sonarr/Radarr instances configured has nothing to
poll or downmix -- the app is fully idle but nothing on the surface says so.
This check warns whenever no :class:`~collapsarr.arr.models.ArrInstance` rows
exist, and reports clean the moment at least one is configured, so a silently
idle install is visible on ``/health`` and the health-check list page (COL-76)
without anyone having to notice the *absence* of activity on their own.

A singleton check (Check Key is just the code, ``instance_id=None``) -- it
reports on the *count* of configured instances, not on any one instance's
state. Contrast with COL-78's per-instance connectivity check, which reports
one result per instance and uses a different Check Code/category.
"""

from __future__ import annotations

from collections.abc import Sequence

from collapsarr.arr.service import list_instances

from .context import HealthCheckContext
from .result import SEVERITY_WARNING, HealthCheckResult

ARR_INSTANCES_CHECK_NAME = "arr_instances"
# Follows the `{SEVERITY}-{CATEGORY}-{SEQ}` Check Code glossary format
# (CONTEXT.md) -- unlike `ffmpeg_missing` (collapsarr/health/ffmpeg.py), which is
# a permanent, one-off exception documented there. This is the first check to
# use the "ARR" category; COL-78's per-instance connectivity check uses its own
# distinct code/category so the two never collide on the same Check Key.
NO_ARR_INSTANCES_CODE = "WARN-ARR-001"
ARR_INSTANCES_CATEGORY = "arr"


def _to_result(instance_count: int) -> HealthCheckResult:
    """Build the singleton result for the current configured-instance count."""
    if instance_count == 0:
        return HealthCheckResult.failed(
            code=NO_ARR_INSTANCES_CODE,
            category=ARR_INSTANCES_CATEGORY,
            severity=SEVERITY_WARNING,
            message=(
                "No Arr (Sonarr/Radarr) instances are configured yet -- "
                "Collapsarr has nothing to poll or downmix."
            ),
        )
    return HealthCheckResult.ok(
        code=NO_ARR_INSTANCES_CODE,
        category=ARR_INSTANCES_CATEGORY,
        severity=SEVERITY_WARNING,
        message=f"{instance_count} Arr instance(s) configured.",
    )


def run_arr_instances_check(context: HealthCheckContext) -> Sequence[HealthCheckResult]:
    """Framework ``run`` callable: warn iff zero Arr instances are configured.

    Reuses :func:`collapsarr.arr.service.list_instances` -- the same query the
    Arr Integration component's own CRUD surface uses -- rather than a
    bespoke count query, so this check and the settings UI never disagree on
    what "configured" means.
    """
    return [_to_result(len(list_instances(context.session)))]


__all__ = [
    "ARR_INSTANCES_CATEGORY",
    "ARR_INSTANCES_CHECK_NAME",
    "NO_ARR_INSTANCES_CODE",
    "run_arr_instances_check",
]
