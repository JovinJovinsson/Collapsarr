"""Tests for the disk-space low/critical health check (COL-79).

``free_space_percent`` and ``_to_results``-driving behaviour are exercised
against an injected fake :class:`~collapsarr.health.disk_space.DiskUsage`
reading (a plain :class:`typing.NamedTuple` matching the ``total``/``free``
fields :func:`shutil.disk_usage` returns) -- no real near-full filesystem is
needed, matching the injection idiom
:func:`~collapsarr.health.ffmpeg.make_ffmpeg_check_run` /
:func:`~collapsarr.health.arr_connectivity.make_arr_connectivity_check_run`
already use.

Both Check Codes (``WARN-DISK-001``, ``ERR-DISK-001``) are singleton
(``instance_id=None``); thresholds are read live off the persisted
:class:`~collapsarr.settings.models.GlobalSettings` row on every call, so a
suite exercising "change takes effect on the next tick, no restart" simply
updates settings between two calls against the same context.
"""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.health import (
    DISK_SPACE_CATEGORY,
    DISK_SPACE_CHECK_NAME,
    DISK_SPACE_ERROR_CODE,
    DISK_SPACE_WARNING_CODE,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    HealthCheckContext,
    default_health_checks,
    free_space_percent,
    make_disk_space_check_run,
)
from collapsarr.settings.service import update_global_settings


class _FakeUsage(NamedTuple):
    total: int
    used: int
    free: int


def _usage_for_percent(percent: float, *, total: int = 1000) -> _FakeUsage:
    """Build a fake disk-usage reading whose free% is exactly ``percent``."""
    free = int(total * percent / 100)
    return _FakeUsage(total=total, used=total - free, free=free)


def _context(settings: Settings, session: Session) -> HealthCheckContext:
    return HealthCheckContext(settings=settings, session=session)


# --- free_space_percent (pure) --------------------------------------------------


def test_free_space_percent_computes_the_ratio() -> None:
    assert free_space_percent(_FakeUsage(total=1000, used=900, free=100)) == 10.0


def test_free_space_percent_handles_a_zero_total_as_fully_free() -> None:
    assert free_space_percent(_FakeUsage(total=0, used=0, free=0)) == 100.0


# --- clean / warning / error boundaries -----------------------------------------


def test_clean_when_free_percent_is_at_or_above_the_warning_threshold(
    settings: Settings, session: Session
) -> None:
    """Default thresholds are 5% warning / 2% error; 10% free is clean on both."""
    run = make_disk_space_check_run(lambda _path: _usage_for_percent(10.0))
    context = _context(settings, session)

    results = run(context)

    assert len(results) == 2
    by_code = {result.code: result for result in results}
    assert by_code[DISK_SPACE_WARNING_CODE].passing is True
    assert by_code[DISK_SPACE_ERROR_CODE].passing is True
    for result in results:
        assert result.category == DISK_SPACE_CATEGORY
        assert result.instance_id is None


def test_warns_when_free_percent_is_below_warning_but_at_or_above_error(
    settings: Settings, session: Session
) -> None:
    """3% free: below the 5% warning threshold, still at/above the 2% error one."""
    run = make_disk_space_check_run(lambda _path: _usage_for_percent(3.0))
    context = _context(settings, session)

    results = run(context)

    by_code = {result.code: result for result in results}
    warning = by_code[DISK_SPACE_WARNING_CODE]
    error = by_code[DISK_SPACE_ERROR_CODE]
    assert warning.passing is False
    assert warning.severity == SEVERITY_WARNING
    assert "3.0%" in warning.message
    assert error.passing is True
    assert error.severity == SEVERITY_ERROR


def test_errors_when_free_percent_is_below_the_error_threshold(
    settings: Settings, session: Session
) -> None:
    """1% free: below both the 5% warning and 2% error thresholds -- both fail."""
    run = make_disk_space_check_run(lambda _path: _usage_for_percent(1.0))
    context = _context(settings, session)

    results = run(context)

    by_code = {result.code: result for result in results}
    warning = by_code[DISK_SPACE_WARNING_CODE]
    error = by_code[DISK_SPACE_ERROR_CODE]
    assert warning.passing is False
    assert error.passing is False
    assert error.severity == SEVERITY_ERROR
    assert "1.0%" in error.message


def test_exact_threshold_percent_is_not_failing(settings: Settings, session: Session) -> None:
    """Free% exactly equal to a threshold passes (strict less-than, not <=)."""
    run = make_disk_space_check_run(lambda _path: _usage_for_percent(5.0))
    context = _context(settings, session)

    results = run(context)

    by_code = {result.code: result for result in results}
    assert by_code[DISK_SPACE_WARNING_CODE].passing is True
    assert by_code[DISK_SPACE_ERROR_CODE].passing is True


# --- live threshold reads (no restart needed) -----------------------------------


def test_changing_thresholds_takes_effect_on_the_next_call(
    settings: Settings, session: Session
) -> None:
    """AC: editing the thresholds takes effect on the very next check call."""
    run = make_disk_space_check_run(lambda _path: _usage_for_percent(4.0))
    context = _context(settings, session)

    before = run(context)
    before_by_code = {result.code: result for result in before}
    assert before_by_code[DISK_SPACE_WARNING_CODE].passing is False  # 4% < 5% default warning

    update_global_settings(session, disk_space_warning_percent=3.0)

    after = run(context)
    after_by_code = {result.code: result for result in after}
    assert after_by_code[DISK_SPACE_WARNING_CODE].passing is True  # 4% >= 3% new warning


# --- default disk_usage probe ---------------------------------------------------


def test_defaults_to_the_real_shutil_disk_usage_probe(
    settings: Settings, session: Session
) -> None:
    """No injected probe: reads the real filesystem backing settings.data_dir."""
    run = make_disk_space_check_run()
    context = _context(settings, session)

    results = run(context)

    assert len(results) == 2
    assert {result.code for result in results} == {DISK_SPACE_WARNING_CODE, DISK_SPACE_ERROR_CODE}


# --- registration ----------------------------------------------------------------


def test_registered_with_default_health_checks() -> None:
    names = [check.name for check in default_health_checks()]

    assert DISK_SPACE_CHECK_NAME in names
