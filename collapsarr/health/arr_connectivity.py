"""Per-instance Arr (Sonarr/Radarr) unreachable health check (COL-78).

For every configured :class:`~collapsarr.arr.models.ArrInstance`, performs a
fresh **live** reachability probe via
:func:`collapsarr.arr.client.check_connectivity` -- the same function
:mod:`collapsarr.arr.service` calls when an instance is created/updated --
rather than reading the ``status``/``status_error`` columns cached on the
instance row from its last save. Those columns go stale the moment an
instance goes down (or comes back up) *between* edits, so a check built on
them would miss exactly the failure this ticket exists to catch.

A per-instance check: it returns one :class:`~collapsarr.health.result.HealthCheckResult`
per configured instance, each keyed on ``(ARR_UNREACHABLE_CODE, instance.id)``.
The Check Code names the check *type* only (unreachable Arr connectivity);
``instance_id`` plus the per-instance ``message`` identify which instance is
failing (CONTEXT.md's Check Key). One instance's failure/recovery therefore
never touches another's persisted state -- :mod:`collapsarr.health.service`
reconciles each ``(code, instance_id)`` key independently. Contrast with
COL-77's ``run_arr_instances_check``, a *singleton* check (``instance_id=None``)
reporting on the configured-instance *count*, not on any one instance's
reachability -- the two checks use distinct Check Codes/categories so they
never collide on the same Check Key.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import httpx

from collapsarr.arr.client import ConnectivityResult, check_connectivity
from collapsarr.arr.models import ArrInstance
from collapsarr.arr.service import list_instances

from .context import HealthCheckContext
from .result import SEVERITY_ERROR, HealthCheckResult

ARR_CONNECTIVITY_CHECK_NAME = "arr_connectivity"
# Follows the `{SEVERITY}-{CATEGORY}-{SEQ}` Check Code glossary format
# (CONTEXT.md) -- CONTEXT.md's own worked example. Distinct from COL-77's
# `WARN-ARR-001` (a different check type: instance count, not connectivity),
# so the two never collide on the same Check Key.
ARR_UNREACHABLE_CODE = "ERR-CONN-001"
ARR_CONNECTIVITY_CATEGORY = "connectivity"


def _to_result(instance: ArrInstance, probe: ConnectivityResult) -> HealthCheckResult:
    """Build this instance's per-tick result from a fresh live probe outcome."""
    if probe.ok:
        return HealthCheckResult.ok(
            code=ARR_UNREACHABLE_CODE,
            category=ARR_CONNECTIVITY_CATEGORY,
            severity=SEVERITY_ERROR,
            message=f"{instance.name!r} is reachable (version {probe.version}).",
            instance_id=instance.id,
        )
    return HealthCheckResult.failed(
        code=ARR_UNREACHABLE_CODE,
        category=ARR_CONNECTIVITY_CATEGORY,
        severity=SEVERITY_ERROR,
        message=f"{instance.name!r} is unreachable: {probe.error}",
        instance_id=instance.id,
    )


def make_arr_connectivity_check_run(
    transport: httpx.BaseTransport | None = None,
) -> Callable[[HealthCheckContext], Sequence[HealthCheckResult]]:
    """Build the framework ``run`` callable for the Arr-unreachable check.

    ``transport`` is forwarded to every :func:`check_connectivity` call this
    check makes (tests inject an ``httpx.MockTransport``; production leaves it
    ``None`` for a real network call), matching the
    :func:`~collapsarr.health.ffmpeg.make_ffmpeg_check_run` injection idiom.
    """

    def run(context: HealthCheckContext) -> Sequence[HealthCheckResult]:
        instances = list_instances(context.session)
        return [
            _to_result(
                instance,
                check_connectivity(instance.base_url, instance.api_key, transport=transport),
            )
            for instance in instances
        ]

    return run


__all__ = [
    "ARR_CONNECTIVITY_CATEGORY",
    "ARR_CONNECTIVITY_CHECK_NAME",
    "ARR_UNREACHABLE_CODE",
    "make_arr_connectivity_check_run",
]
