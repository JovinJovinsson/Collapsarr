"""The shared health-check result shape and severity vocabulary (COL-75).

This is the one datatype every health check speaks in, and the seam the whole
Health Check Framework (Epic COL-74) is built around. A check runs and returns
zero or more :class:`HealthCheckResult` values -- one per *Check Key* it is
responsible for -- each self-describing:

- ``code`` -- the stable *Check Code* (see ``CONTEXT.md``): the wiki-searchable
  identifier for this check's failure state, fixed at authoring time. Shared
  across every instance that fails the same check.
- ``category`` -- a coarse grouping tag (e.g. ``"ffmpeg"``, ``"connectivity"``,
  ``"disk"``) for filtering/reporting in the UI.
- ``severity`` -- exactly ``"warning"`` or ``"error"``; fixed per code, never
  computed from live state.
- ``message`` -- the current human-readable detail (may differ between a
  passing and a failing run of the same check).
- ``passing`` -- whether the check is *currently* satisfied. A passing result
  still carries the code/category/severity of the failure it would raise, so
  the framework can key persisted state on ``(code, instance_id)`` and detect a
  pass->fail / fail->pass transition against the previous run.
- ``instance_id`` -- ``None`` for a singleton check (FFmpeg, disk, database),
  or the Arr instance id for a per-instance check (connectivity). Together with
  ``code`` it forms the *Check Key* that disambiguates which occurrence failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

Severity = Literal["warning", "error"]

SEVERITY_WARNING: Final = "warning"
SEVERITY_ERROR: Final = "error"


@dataclass(frozen=True, slots=True)
class HealthCheckResult:
    """One check's outcome for a single *Check Key* on a single run.

    Immutable and self-describing: the scheduler collects the results of every
    registered check each tick and reconciles them against persisted state
    purely from these fields (see :mod:`collapsarr.health.service`).
    """

    code: str
    category: str
    severity: Severity
    message: str
    passing: bool
    instance_id: int | None = None

    @property
    def key(self) -> tuple[str, int | None]:
        """The *Check Key* ``(code, instance_id)`` this result is state-tracked by."""
        return (self.code, self.instance_id)

    @classmethod
    def ok(
        cls,
        *,
        code: str,
        category: str,
        severity: Severity,
        message: str,
        instance_id: int | None = None,
    ) -> HealthCheckResult:
        """Build a *passing* result for the given Check Key."""
        return cls(
            code=code,
            category=category,
            severity=severity,
            message=message,
            passing=True,
            instance_id=instance_id,
        )

    @classmethod
    def failed(
        cls,
        *,
        code: str,
        category: str,
        severity: Severity,
        message: str,
        instance_id: int | None = None,
    ) -> HealthCheckResult:
        """Build a *failing* result for the given Check Key."""
        return cls(
            code=code,
            category=category,
            severity=severity,
            message=message,
            passing=False,
            instance_id=instance_id,
        )
