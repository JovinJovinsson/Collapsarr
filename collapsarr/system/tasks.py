"""HTTP REST endpoint aggregating Collapsarr's background schedulers (COL-122).

A thin, read-only aggregation layer over the four independent scheduler
classes -- :class:`~collapsarr.jobs.scheduler.JobScheduler` (library scan),
:class:`~collapsarr.health.scheduler.HealthCheckScheduler` (health checks),
:class:`~collapsarr.backup.scheduler.BackupScheduler` (backups), and
:class:`~collapsarr.update_check.scheduler.UpdateCheckScheduler` (update
check) -- exposed as a FastAPI :class:`~fastapi.APIRouter` mounted under
``/api/system`` by :func:`collapsarr.main.create_app`, inheriting the same
auth gate every other ``/api`` route does.

Per ``docs/adr/0005-system-tasks-endpoint-not-shared-scheduler.md``, this
endpoint deliberately reimplements each scheduler's own "next run" logic
against its existing state (settings-backed intervals, persisted/in-memory
last-run timestamps) rather than refactor the four into a shared
``BaseScheduler``/registry -- see the ADR for the accepted duplication
trade-off.

Endpoint:

* ``GET /api/system/tasks`` -- one :class:`ScheduledTaskRead` row per
  Scheduled Task (``CONTEXT.md``'s "Scheduled Task"), in a fixed order:
  Library scan, Health checks, Backups, Update check. ``scheduler_enabled``
  reflects whether *that task's* background loop is actually running right
  now; when it is ``False``, or the task has not completed a run yet this
  process (``last_run_at`` is ``None``), ``next_run_at`` is reported
  ``None`` rather than a computed time that will never fire.

Each row's manual "Run now" action is **not** a new endpoint here -- the
frontend calls that task's existing manual-trigger endpoint unchanged
(``POST /api/jobs/scan``, ``POST /api/system/health-checks/recheck``,
``POST /api/system/backup``, ``POST /api/system/updates/recheck``) and
refetches this list, per the ticket's acceptance criteria.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..backup.scheduler import BackupScheduler
from ..backup.service import BACKUP_SCHEDULED, list_backups
from ..config import Settings
from ..health.scheduler import INTERVAL_SECONDS as HEALTH_CHECK_INTERVAL_SECONDS
from ..health.service import list_health_check_states
from ..jobs.scheduler import JobScheduler
from ..settings.service import get_global_settings
from ..update_check.scheduler import INTERVAL_SECONDS as UPDATE_CHECK_INTERVAL_SECONDS
from ..update_check.service import get_update_check_state

router = APIRouter(prefix="/api/system", tags=["system"])


# --- schemas -----------------------------------------------------------------


class ScheduledTaskRead(BaseModel):
    """One row of the Scheduled Task registry (``CONTEXT.md``'s "Scheduled Task").

    ``next_run_at``/``last_run_at`` are ``None`` when unavailable -- either
    the scheduler for this task isn't currently running
    (``scheduler_enabled`` is ``False``) or it has not completed a run yet
    this process. ``interval_label`` is always populated (it's derived from
    configuration, not run state), so the row is still informative even
    before the first run.
    """

    name: str
    interval_label: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    scheduler_enabled: bool


# --- helpers -------------------------------------------------------------


def _format_interval(value: float, unit: str) -> str:
    """Render a cadence like ``"Every 6 hours"`` / ``"Every 1 day"``.

    ``value`` is formatted with ``:g`` so a whole-number float (the common
    case -- :attr:`~collapsarr.config.Settings.scan_interval_hours` defaults
    to ``6.0``) prints as ``"6"`` rather than ``"6.0"``, while a genuinely
    fractional interval still renders (``"6.5"``). Pluralises ``unit``
    unless ``value`` is exactly ``1``.
    """
    return f"Every {value:g} {unit}{'' if value == 1 else 's'}"


def _next_run_at(
    *, scheduler_enabled: bool, last_run_at: datetime | None, interval: timedelta
) -> datetime | None:
    """Compute a task's next-run time, or ``None`` when it wouldn't be meaningful.

    ``None`` whenever ``scheduler_enabled`` is ``False`` (the background
    loop isn't running, so nothing will fire) or ``last_run_at`` is ``None``
    (nothing to add the interval to yet) -- the two cases the ticket's
    acceptance criteria call out explicitly.
    """
    if not scheduler_enabled or last_run_at is None:
        return None
    return last_run_at + interval


def _library_scan_task(request: Request) -> ScheduledTaskRead:
    """Build the Library Scan row from :class:`~collapsarr.jobs.scheduler.JobScheduler`.

    ``interval_label`` is derived from ``Settings.scan_interval_hours``
    regardless of whether a scheduler is actually wired (it's config, not
    run state). The scheduler itself is only wired on
    ``app.state.job_scheduler`` when the app was built with
    ``enable_scheduler=True`` (see :func:`collapsarr.main.create_app`) --
    when it's absent this task has never run and cannot run, so
    ``scheduler_enabled`` is ``False`` and both timestamps are ``None``.
    """
    settings: Settings = request.app.state.settings
    scheduler: JobScheduler | None = getattr(request.app.state, "job_scheduler", None)
    scheduler_enabled = scheduler is not None
    last_run_at = scheduler.last_scan_at if scheduler is not None else None
    interval = timedelta(hours=settings.scan_interval_hours)
    return ScheduledTaskRead(
        name="Library scan",
        interval_label=_format_interval(settings.scan_interval_hours, "hour"),
        next_run_at=_next_run_at(
            scheduler_enabled=scheduler_enabled, last_run_at=last_run_at, interval=interval
        ),
        last_run_at=last_run_at,
        scheduler_enabled=scheduler_enabled,
    )


def _health_checks_task(request: Request, session: Session) -> ScheduledTaskRead:
    """Build the Health Checks row from persisted health-check state rows.

    Unlike Library Scan/Backups,
    :class:`~collapsarr.health.scheduler.HealthCheckScheduler` is wired on
    ``app.state.health_scheduler`` *unconditionally* (its first tick always
    runs synchronously at app startup so ``/health`` is accurate
    immediately -- see :func:`collapsarr.main.create_app`'s lifespan), so
    its presence can't tell us whether the periodic background loop is
    actually running. ``scheduler_enabled`` here instead reflects the
    app-wide ``enable_scheduler`` flag directly
    (``app.state.enable_scheduler``), matching the condition the lifespan
    itself gates ``health_scheduler.start()`` on. ``last_run_at`` is still
    reported whenever available (even with the loop disabled) -- only
    ``next_run_at`` depends on ``scheduler_enabled``.
    """
    scheduler_enabled = bool(getattr(request.app.state, "enable_scheduler", False))
    states = list_health_check_states(session)
    last_run_at = max((state.last_checked_at for state in states), default=None)
    interval = timedelta(seconds=HEALTH_CHECK_INTERVAL_SECONDS)
    return ScheduledTaskRead(
        name="Health checks",
        interval_label=_format_interval(HEALTH_CHECK_INTERVAL_SECONDS / 60.0, "minute"),
        next_run_at=_next_run_at(
            scheduler_enabled=scheduler_enabled, last_run_at=last_run_at, interval=interval
        ),
        last_run_at=last_run_at,
        scheduler_enabled=scheduler_enabled,
    )


def _backups_task(request: Request, session: Session) -> ScheduledTaskRead:
    """Build the Backups row from the newest on-disk ``scheduled`` archive.

    Mirrors :class:`~collapsarr.backup.scheduler.BackupScheduler`'s on-disk
    due-ness independently (see ``docs/adr/0005-...``) rather than calling
    its private methods directly. ``scheduler_enabled`` reflects whether
    :class:`~collapsarr.backup.scheduler.BackupScheduler` is actually wired
    on ``app.state.backup_scheduler`` (gated on ``enable_scheduler`` alone,
    same as Library Scan) -- unlike Health Checks/Update Check, it is not
    wired at all when the app was built without the scheduler.
    """
    settings: Settings = request.app.state.settings
    scheduler: BackupScheduler | None = getattr(request.app.state, "backup_scheduler", None)
    scheduler_enabled = scheduler is not None
    scheduled_backups = [info for info in list_backups(settings) if info.type == BACKUP_SCHEDULED]
    last_run_at = max((info.created_at for info in scheduled_backups), default=None)
    interval_days = get_global_settings(session).backup_interval_days
    interval = timedelta(days=interval_days)
    return ScheduledTaskRead(
        name="Backups",
        interval_label=_format_interval(interval_days, "day"),
        next_run_at=_next_run_at(
            scheduler_enabled=scheduler_enabled, last_run_at=last_run_at, interval=interval
        ),
        last_run_at=last_run_at,
        scheduler_enabled=scheduler_enabled,
    )


def _update_check_task(request: Request, session: Session) -> ScheduledTaskRead:
    """Build the Update Check row from the singleton Update Check state row.

    Wired unconditionally, same as Health Checks -- see
    :func:`_health_checks_task`'s docstring for why ``scheduler_enabled``
    reads the app-wide ``enable_scheduler`` flag instead of the scheduler
    object's presence.
    """
    scheduler_enabled = bool(getattr(request.app.state, "enable_scheduler", False))
    state = get_update_check_state(session)
    last_run_at = state.checked_at if state is not None else None
    interval = timedelta(seconds=UPDATE_CHECK_INTERVAL_SECONDS)
    return ScheduledTaskRead(
        name="Update check",
        interval_label=_format_interval(UPDATE_CHECK_INTERVAL_SECONDS / 3600.0, "hour"),
        next_run_at=_next_run_at(
            scheduler_enabled=scheduler_enabled, last_run_at=last_run_at, interval=interval
        ),
        last_run_at=last_run_at,
        scheduler_enabled=scheduler_enabled,
    )


# --- endpoints ---------------------------------------------------------------


@router.get("/tasks", response_model=list[ScheduledTaskRead])
def list_tasks_endpoint(request: Request) -> list[ScheduledTaskRead]:
    """List every Scheduled Task with its cadence, next/last run, and enabled state.

    Fixed order: Library scan, Health checks, Backups, Update check -- matches
    the order the ticket/ADR describe the four schedulers in, giving the
    frontend table a stable row order across requests.
    """
    with request.app.state.session_factory() as session:
        return [
            _library_scan_task(request),
            _health_checks_task(request, session),
            _backups_task(request, session),
            _update_check_task(request, session),
        ]


__all__ = ["ScheduledTaskRead", "router"]
