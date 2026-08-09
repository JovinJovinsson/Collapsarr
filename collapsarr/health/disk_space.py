"""Disk-space low/critical health check (COL-79).

A **two-tier** singleton check against the free-space percentage of the
filesystem backing the app's own data directory
(:attr:`~collapsarr.config.Settings.data_dir`), using the stdlib
:func:`shutil.disk_usage` -- no new dependency.

Unlike every other COL-74 check so far, this one has two distinct failure
*severities* for what is, at the domain level, a single underlying
measurement (live free-space percentage): a **warning** below a configurable
percentage, escalating to an **error** below a second, lower configurable
percentage. ``CONTEXT.md``'s Check Code glossary is explicit that a Check
Code's severity is fixed at authoring time and never varies per run, and that
"a check that can present at two severities... gets two distinct codes, not
one code with a variable severity." Accordingly this module defines **two**
Check Codes -- ``WARN-DISK-001`` and ``ERR-DISK-001`` -- and
:func:`make_disk_space_check_run` returns **two** :class:`~collapsarr.health.
result.HealthCheckResult` values every tick, one per code, each independently
passing/failing against its own threshold:

- ``WARN-DISK-001`` fails whenever free% < ``disk_space_warning_percent``.
- ``ERR-DISK-001`` fails whenever free% < ``disk_space_error_percent``.

Both are singleton Check Keys (``instance_id=None``, matching FFmpeg). Because
the two are independent (not "error implies warning is suppressed"), crossing
below the (lower) error threshold also still fails the warning code -- both
report failing simultaneously, which is the correct "this is now doubly true"
reading rather than losing the warning the moment the error fires. The two
thresholds are read **live** from the persisted
:class:`~collapsarr.settings.models.GlobalSettings` row
(:func:`collapsarr.settings.service.get_global_settings`) on every call, so an
operator edit via Settings takes effect on the very next scheduler tick with
no restart -- there is no process-cached copy of either threshold anywhere in
this module.

The two threshold fields are stored and validated independently; nothing
enforces ``disk_space_error_percent < disk_space_warning_percent`` at the
model, service, or API layer (see ``collapsarr/settings/service.py``). An
operator who inverts them gets exactly what the independent per-code
comparisons above imply -- e.g. only the error code trips, never the warning
-- rather than a rejected save; documented as a deliberate, minimal-surface
choice, not an oversight.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from typing import Protocol

from collapsarr.settings.service import get_global_settings

from .context import HealthCheckContext
from .result import SEVERITY_ERROR, SEVERITY_WARNING, HealthCheckResult


class DiskUsage(Protocol):
    """The subset of :func:`shutil.disk_usage`'s return value this check needs.

    Structurally matches the ``usage(total, used, free)`` named tuple
    :func:`shutil.disk_usage` returns, without depending on that (private)
    named-tuple type directly -- lets tests inject a plain stand-in (e.g. a
    :class:`typing.NamedTuple` or a tiny dataclass) instead of a real
    filesystem probe. Declared as read-only properties (not plain
    attributes) so a :class:`typing.NamedTuple` -- whose fields are
    read-only -- satisfies this Protocol structurally.
    """

    @property
    def total(self) -> int: ...  # noqa: D102 - trivial Protocol accessor

    @property
    def free(self) -> int: ...  # noqa: D102 - trivial Protocol accessor


DISK_SPACE_CHECK_NAME = "disk_space"
# Follows the `{SEVERITY}-{CATEGORY}-{SEQ}` Check Code glossary format
# (CONTEXT.md). Two codes for this one check -- see the module docstring for
# why a two-severity check needs two fixed-severity codes rather than one
# code with a variable severity. Distinct from COL-77's `WARN-ARR-001` and
# COL-78's `ERR-CONN-001` (different category, "disk" vs. "arr"/"connectivity"),
# so neither Check Key can ever collide with this check's.
DISK_SPACE_WARNING_CODE = "WARN-DISK-001"
DISK_SPACE_ERROR_CODE = "ERR-DISK-001"
DISK_SPACE_CATEGORY = "disk"


def free_space_percent(usage: DiskUsage) -> float:
    """Return the free-space percentage of a :class:`DiskUsage` reading.

    A ``total`` of zero (a degenerate/empty filesystem report) reads as 100%
    free rather than raising a division error -- there is no meaningful "low
    space" condition on a filesystem reporting no capacity at all.
    """
    if usage.total <= 0:
        return 100.0
    return (usage.free / usage.total) * 100.0


def _warning_result(
    free_percent: float, *, warning_percent: float, data_dir: str
) -> HealthCheckResult:
    if free_percent < warning_percent:
        return HealthCheckResult.failed(
            code=DISK_SPACE_WARNING_CODE,
            category=DISK_SPACE_CATEGORY,
            severity=SEVERITY_WARNING,
            message=(
                f"Only {free_percent:.1f}% free space remains on the filesystem "
                f"backing the data directory ({data_dir!r}) -- below the "
                f"{warning_percent:g}% warning threshold."
            ),
        )
    return HealthCheckResult.ok(
        code=DISK_SPACE_WARNING_CODE,
        category=DISK_SPACE_CATEGORY,
        severity=SEVERITY_WARNING,
        message=(
            f"{free_percent:.1f}% free space remains on the filesystem backing "
            f"the data directory ({data_dir!r})."
        ),
    )


def _error_result(free_percent: float, *, error_percent: float, data_dir: str) -> HealthCheckResult:
    if free_percent < error_percent:
        return HealthCheckResult.failed(
            code=DISK_SPACE_ERROR_CODE,
            category=DISK_SPACE_CATEGORY,
            severity=SEVERITY_ERROR,
            message=(
                f"Only {free_percent:.1f}% free space remains on the filesystem "
                f"backing the data directory ({data_dir!r}) -- below the "
                f"{error_percent:g}% critical threshold."
            ),
        )
    return HealthCheckResult.ok(
        code=DISK_SPACE_ERROR_CODE,
        category=DISK_SPACE_CATEGORY,
        severity=SEVERITY_ERROR,
        message=(
            f"{free_percent:.1f}% free space remains on the filesystem backing "
            f"the data directory ({data_dir!r})."
        ),
    )


def _default_disk_usage(path: str) -> DiskUsage:
    """Adapt :func:`shutil.disk_usage` to the narrower :class:`DiskUsage` shape.

    A thin, explicitly-typed wrapper -- rather than passing
    ``shutil.disk_usage`` directly wherever a ``Callable[[str], DiskUsage]``
    is expected -- so mypy checks the real return type
    (``shutil._ntuple_diskusage``) structurally against :class:`DiskUsage`
    exactly once, here, instead of failing to unify the two distinct callable
    types at every call site that falls back to the real probe.
    """
    return shutil.disk_usage(path)


def _to_results(
    free_percent: float, *, warning_percent: float, error_percent: float, data_dir: str
) -> list[HealthCheckResult]:
    """Build both Check Key results (warning tier, error tier) for one reading."""
    return [
        _warning_result(free_percent, warning_percent=warning_percent, data_dir=data_dir),
        _error_result(free_percent, error_percent=error_percent, data_dir=data_dir),
    ]


def get_disk_usage(
    data_dir: str, disk_usage: Callable[[str], DiskUsage] | None = None
) -> DiskUsage:
    """Return the raw :class:`DiskUsage` reading (total/free bytes) for ``data_dir``.

    Exposes the same probe :func:`make_disk_space_check_run`'s returned
    callable reads percentages from, so a caller that wants the raw byte
    counts -- rather than a warning/error
    :class:`~collapsarr.health.result.HealthCheckResult` pair -- doesn't need
    to re-invoke :func:`shutil.disk_usage` itself. The About-panel endpoint
    (``GET /api/system/info``, :mod:`collapsarr.system.info`, COL-123) is the
    first such caller: it reuses this to report ``disk.free_bytes``/
    ``disk.total_bytes`` off the exact same reading -- and the exact same
    ``disk_usage`` test-injection seam -- the health check itself uses,
    instead of a second, independent filesystem probe.
    """
    probe = disk_usage or _default_disk_usage
    return probe(data_dir)


def make_disk_space_check_run(
    disk_usage: Callable[[str], DiskUsage] | None = None,
) -> Callable[[HealthCheckContext], Sequence[HealthCheckResult]]:
    """Build the framework ``run`` callable for the disk-space check.

    ``disk_usage`` overrides the probe (defaults to the real
    :func:`shutil.disk_usage`), letting tests report an arbitrary free-space
    percentage without needing a filesystem actually near-full. Matches the
    ``checker`` injection idiom :func:`~collapsarr.health.ffmpeg.
    make_ffmpeg_check_run` and :func:`~collapsarr.health.arr_connectivity.
    make_arr_connectivity_check_run` already use.

    The returned callable reads ``disk_space_warning_percent``/
    ``disk_space_error_percent`` off the persisted
    :class:`~collapsarr.settings.models.GlobalSettings` row fresh on every
    call (via ``context.session``), and probes
    ``context.settings.data_dir`` fresh on every call (via
    :func:`get_disk_usage`) -- so both the thresholds and the measurement are
    always current-tick, never cached.
    """

    def run(context: HealthCheckContext) -> Sequence[HealthCheckResult]:
        global_settings = get_global_settings(context.session)
        data_dir = context.settings.data_dir
        usage = get_disk_usage(data_dir, disk_usage)
        percent = free_space_percent(usage)
        return _to_results(
            percent,
            warning_percent=global_settings.disk_space_warning_percent,
            error_percent=global_settings.disk_space_error_percent,
            data_dir=data_dir,
        )

    return run


__all__ = [
    "DISK_SPACE_CATEGORY",
    "DISK_SPACE_CHECK_NAME",
    "DISK_SPACE_ERROR_CODE",
    "DISK_SPACE_WARNING_CODE",
    "DiskUsage",
    "free_space_percent",
    "get_disk_usage",
    "make_disk_space_check_run",
]
