"""Failed-jobs-backing-up health check (COL-81).

A downmix pipeline that keeps failing (a bad FFmpeg build, a systematically
unreachable remote path, a corrupt-media pattern in the library) produces no
loud single failure -- each job just quietly lands in
:class:`~collapsarr.jobs.models.JobHistory` as ``FAILED`` and the operator has
to notice the *pattern* on their own. This check does that noticing for them:
it counts how many :class:`~collapsarr.jobs.models.JobHistory` rows reached
``FAILED`` within a rolling 24-hour window and warns once that count reaches
5, using nothing but the existing job-history table COL-21 already persists
-- no new model, no new write path.

A singleton check (Check Key is just the code, ``instance_id=None``): it
reports on the aggregate failure *rate* across every job, not on any one
job or file. Single-tier (warning only, unlike COL-79's two-tier disk-space
check) -- there is no escalation to ``error`` for this check; see
``CONTEXT.md``'s Check Code glossary on why a two-severity check would need
two distinct codes rather than one code with a variable severity, which
doesn't apply here since this check only ever has the one severity.

The rolling window is evaluated fresh on every call against ``ended_at``
(the timestamp :meth:`~collapsarr.jobs.queue.JobQueue._run_job` stamps when a
job reaches its terminal status) -- never cached -- so a burst of failures
that ages out of the last 24 hours clears the warning on a later tick without
any explicit "dismiss" action, matching COL-77's no-Arr-instances check's
"clears itself the moment the condition resolves" shape.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collapsarr.jobs.models import JobHistory
from collapsarr.jobs.queue import JobStatus

from .context import HealthCheckContext
from .result import SEVERITY_WARNING, HealthCheckResult

FAILED_JOBS_CHECK_NAME = "failed_jobs"
# Follows the `{SEVERITY}-{CATEGORY}-{SEQ}` Check Code glossary format
# (CONTEXT.md). "jobs" is a new category, distinct from COL-77's
# `WARN-ARR-001` ("arr"), COL-78's `ERR-CONN-001` ("connectivity"), COL-79's
# `WARN-DISK-001`/`ERR-DISK-001` ("disk"), and COL-80's `ERR-DB-001` ("db") --
# this Check Key can never collide with any of those.
FAILED_JOBS_BACKING_UP_CODE = "WARN-JOBS-001"
FAILED_JOBS_CATEGORY = "jobs"

#: 5+ failures in the rolling window trips the warning (AC).
FAILURE_THRESHOLD = 5
#: The rolling lookback window the failure count is evaluated against.
ROLLING_WINDOW = timedelta(hours=24)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def count_recent_failures(session: Session, *, since: datetime) -> int:
    """Count ``JobHistory`` rows that reached ``FAILED`` at/after ``since``.

    Filters on ``ended_at`` (rather than ``started_at`` or ``created_at``) --
    the timestamp stamped when a job actually reached its terminal status --
    so a job that failed just now counts even if it was originally enqueued
    well outside the window. A ``FAILED`` row with no ``ended_at`` (should not
    occur in practice -- :meth:`~collapsarr.jobs.queue.JobQueue._run_job`
    always stamps it before persisting a terminal status -- but not assumed
    here) is never counted, since there is no timestamp to place it in the
    window with.
    """
    stmt = select(func.count()).select_from(JobHistory).where(
        JobHistory.status == JobStatus.FAILED,
        JobHistory.ended_at.is_not(None),
        JobHistory.ended_at >= since,
    )
    return session.scalar(stmt) or 0


def _to_result(failure_count: int) -> HealthCheckResult:
    """Build the singleton result for the current rolling-window failure count."""
    if failure_count >= FAILURE_THRESHOLD:
        return HealthCheckResult.failed(
            code=FAILED_JOBS_BACKING_UP_CODE,
            category=FAILED_JOBS_CATEGORY,
            severity=SEVERITY_WARNING,
            message=(
                f"{failure_count} downmix jobs have failed in the last 24 hours -- "
                f"at or above the {FAILURE_THRESHOLD} warning threshold."
            ),
        )
    return HealthCheckResult.ok(
        code=FAILED_JOBS_BACKING_UP_CODE,
        category=FAILED_JOBS_CATEGORY,
        severity=SEVERITY_WARNING,
        message=f"{failure_count} downmix job(s) failed in the last 24 hours.",
    )


def make_failed_jobs_check_run(
    now: Callable[[], datetime] | None = None,
) -> Callable[[HealthCheckContext], Sequence[HealthCheckResult]]:
    """Build the framework ``run`` callable for the failed-jobs check.

    ``now`` overrides the clock used to compute the rolling window's start
    (defaults to real UTC now), matching the injectable-clock idiom
    :class:`~collapsarr.health.scheduler.HealthCheckScheduler` already uses --
    tests pass a fixed clock to place failures precisely inside/outside the
    window without real wall-clock waits.
    """
    clock = now or _utcnow

    def run(context: HealthCheckContext) -> Sequence[HealthCheckResult]:
        since = clock() - ROLLING_WINDOW
        failure_count = count_recent_failures(context.session, since=since)
        return [_to_result(failure_count)]

    return run


__all__ = [
    "FAILED_JOBS_BACKING_UP_CODE",
    "FAILED_JOBS_CATEGORY",
    "FAILED_JOBS_CHECK_NAME",
    "FAILURE_THRESHOLD",
    "ROLLING_WINDOW",
    "count_recent_failures",
    "make_failed_jobs_check_run",
]
