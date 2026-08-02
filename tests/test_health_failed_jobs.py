"""Tests for the failed-jobs-backing-up health check (COL-81).

``JobHistory`` rows are inserted directly against the ``session`` fixture
(rather than driven through a real ``JobQueue`` run, as
``test_jobs_history.py`` does) -- this check only reads ``status``/
``ended_at`` off the row, and inserting directly lets each test place a
failure's ``ended_at`` at an exact offset from the injected clock, which is
the whole point of the rolling-24-hour-window assertions below. Follows the
injectable-clock idiom :class:`~collapsarr.health.scheduler.HealthCheckScheduler`
and its tests already use.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.health import (
    FAILED_JOBS_BACKING_UP_CODE,
    FAILED_JOBS_CATEGORY,
    FAILED_JOBS_CHECK_NAME,
    FAILURE_THRESHOLD,
    SEVERITY_WARNING,
    HealthCheckContext,
    default_health_checks,
    make_failed_jobs_check_run,
)
from collapsarr.jobs.models import JobHistory
from collapsarr.jobs.queue import JobStatus

_FIXED_NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


def _add_failure(session: Session, *, ended_at: datetime) -> None:
    """Insert one FAILED JobHistory row that ended at exactly ``ended_at``."""
    session.add(
        JobHistory(
            job_id=str(uuid4()),
            file_path="/media/movie.mkv",
            status=JobStatus.FAILED,
            started_at=ended_at - timedelta(minutes=1),
            ended_at=ended_at,
        )
    )
    session.commit()


def _add_succeeded(session: Session, *, ended_at: datetime) -> None:
    """Insert one SUCCEEDED row -- must never count toward the failure total."""
    session.add(
        JobHistory(
            job_id=str(uuid4()),
            file_path="/media/other.mkv",
            status=JobStatus.SUCCEEDED,
            started_at=ended_at - timedelta(minutes=1),
            ended_at=ended_at,
        )
    )
    session.commit()


def _context(settings: Settings, session: Session) -> HealthCheckContext:
    return HealthCheckContext(settings=settings, session=session)


def _run(session: Session, settings: Settings, *, recent_failures: int, now: datetime = _FIXED_NOW):
    run = make_failed_jobs_check_run(lambda: now)
    for _ in range(recent_failures):
        _add_failure(session, ended_at=now - timedelta(hours=1))
    return run(_context(settings, session))


# ---------------------------------------------------------------------------
# Threshold boundaries.
# ---------------------------------------------------------------------------


def test_clean_with_zero_failures(settings: Settings, session: Session) -> None:
    run = make_failed_jobs_check_run(lambda: _FIXED_NOW)

    results = run(_context(settings, session))

    assert len(results) == 1
    result = results[0]
    assert result.passing is True
    assert result.code == FAILED_JOBS_BACKING_UP_CODE
    assert result.category == FAILED_JOBS_CATEGORY
    assert result.severity == SEVERITY_WARNING
    assert result.instance_id is None
    assert "0 downmix job" in result.message


def test_clean_just_under_the_threshold(settings: Settings, session: Session) -> None:
    """4 failures inside the window: below the 5-failure threshold -- clean."""
    assert FAILURE_THRESHOLD == 5
    results = _run(session, settings, recent_failures=4)

    assert results[0].passing is True


def test_warns_at_the_threshold(settings: Settings, session: Session) -> None:
    """Exactly 5 failures inside the window trips the warning (AC)."""
    results = _run(session, settings, recent_failures=5)

    result = results[0]
    assert result.passing is False
    assert result.code == FAILED_JOBS_BACKING_UP_CODE
    assert result.severity == SEVERITY_WARNING
    assert "5 downmix jobs" in result.message


def test_warns_above_the_threshold(settings: Settings, session: Session) -> None:
    results = _run(session, settings, recent_failures=7)

    assert results[0].passing is False


# ---------------------------------------------------------------------------
# Only FAILED rows count.
# ---------------------------------------------------------------------------


def test_succeeded_jobs_never_count_toward_the_failure_total(
    settings: Settings, session: Session
) -> None:
    for _ in range(10):
        _add_succeeded(session, ended_at=_FIXED_NOW - timedelta(hours=1))
    run = make_failed_jobs_check_run(lambda: _FIXED_NOW)

    results = run(_context(settings, session))

    assert results[0].passing is True


# ---------------------------------------------------------------------------
# Rolling 24-hour window: failures age out.
# ---------------------------------------------------------------------------


def test_failures_older_than_24_hours_do_not_count(
    settings: Settings, session: Session
) -> None:
    """5 failures placed *outside* the window: still clean."""
    run = make_failed_jobs_check_run(lambda: _FIXED_NOW)
    for _ in range(5):
        _add_failure(session, ended_at=_FIXED_NOW - timedelta(hours=25))

    results = run(_context(settings, session))

    assert results[0].passing is True
    assert "0 downmix job" in results[0].message


def test_a_mix_of_in_window_and_aged_out_failures_only_counts_the_recent_ones(
    settings: Settings, session: Session
) -> None:
    """4 recent + 3 aged-out: only the 4 recent count -- still under threshold."""
    run = make_failed_jobs_check_run(lambda: _FIXED_NOW)
    for _ in range(4):
        _add_failure(session, ended_at=_FIXED_NOW - timedelta(hours=1))
    for _ in range(3):
        _add_failure(session, ended_at=_FIXED_NOW - timedelta(hours=48))

    results = run(_context(settings, session))

    assert results[0].passing is True
    assert "4 downmix job" in results[0].message


def test_a_later_tick_clears_the_warning_once_failures_age_out(
    settings: Settings, session: Session
) -> None:
    """AC: the warning clears on a later tick once the window no longer holds 5+.

    5 failures all land 23 hours before the first tick (so they're inside the
    window then); the second tick's clock has advanced 2 hours, pushing all
    five to 25 hours old -- outside the window -- with no new failures added.
    """
    for _ in range(5):
        _add_failure(session, ended_at=_FIXED_NOW - timedelta(hours=23))
    context = _context(settings, session)

    first_run = make_failed_jobs_check_run(lambda: _FIXED_NOW)
    before = first_run(context)
    assert before[0].passing is False

    later = _FIXED_NOW + timedelta(hours=2)
    second_run = make_failed_jobs_check_run(lambda: later)
    after = second_run(context)
    assert after[0].passing is True


# ---------------------------------------------------------------------------
# Default clock + registration.
# ---------------------------------------------------------------------------


def test_defaults_to_the_real_utc_clock(settings: Settings, session: Session) -> None:
    """No injected clock: uses real UTC now, so a just-failed job counts."""
    run = make_failed_jobs_check_run()
    _add_failure(session, ended_at=datetime.now(UTC))

    results = run(_context(settings, session))

    assert results[0].passing is True  # only 1 failure, still under threshold
    assert "1 downmix job" in results[0].message


def test_registered_with_default_health_checks() -> None:
    names = [check.name for check in default_health_checks()]

    assert FAILED_JOBS_CHECK_NAME in names
