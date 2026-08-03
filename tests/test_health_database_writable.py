"""Tests for the database-unwritable health check (COL-80).

``run_database_writable_check`` is exercised against the real schema-
initialised ``session`` fixture (a genuine SQLite-backed session, migrated to
head via :func:`collapsarr.migrations.upgrade_to_head` -- see
``tests/conftest.py``) so the "healthy write succeeding" path performs an
actual write-and-commit against the dedicated
:class:`~collapsarr.health.models.HealthWriteProbe` table, not a mock.

The write-failure path is simulated the way the ticket suggests: a
``monkeypatch``-installed session/commit double that raises, rather than an
actually-corrupted database file -- matching this repo's existing
``monkeypatch``-based DB-failure-simulation idiom (see
``tests/test_migration_backup.py``'s forced migration failure). This keeps
the test fast, deterministic, and independent of any real filesystem
permission/locking setup.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.health import (
    DATABASE_UNWRITABLE_CODE,
    DATABASE_WRITABLE_CATEGORY,
    DATABASE_WRITABLE_CHECK_NAME,
    SEVERITY_ERROR,
    WRITE_PROBE_ID,
    HealthCheckContext,
    HealthWriteProbe,
    default_health_checks,
    run_database_writable_check,
)


def _context(settings: Settings, session: Session) -> HealthCheckContext:
    return HealthCheckContext(settings=settings, session=session)


# --- healthy write ----------------------------------------------------------


def test_healthy_write_succeeds_and_creates_the_probe_row(
    settings: Settings, session: Session
) -> None:
    results = run_database_writable_check(_context(settings, session))

    assert len(results) == 1
    result = results[0]
    assert result.code == DATABASE_UNWRITABLE_CODE
    assert result.category == DATABASE_WRITABLE_CATEGORY
    assert result.severity == SEVERITY_ERROR
    assert result.passing is True
    assert result.instance_id is None

    # The write-and-commit actually happened: the dedicated probe row exists.
    probe = session.get(HealthWriteProbe, WRITE_PROBE_ID)
    assert probe is not None
    assert probe.pinged_at is not None


def test_a_second_healthy_tick_updates_the_same_row_rather_than_inserting_again(
    settings: Settings, session: Session
) -> None:
    """AC: writes-and-commits *every* tick -- the second tick is a genuine
    UPDATE against the same singleton row, not a fresh insert."""
    context = _context(settings, session)

    run_database_writable_check(context)
    first_pinged_at = session.get(HealthWriteProbe, WRITE_PROBE_ID).pinged_at  # type: ignore[union-attr]

    run_database_writable_check(context)

    assert session.query(HealthWriteProbe).count() == 1
    second_pinged_at = session.get(HealthWriteProbe, WRITE_PROBE_ID).pinged_at  # type: ignore[union-attr]
    assert second_pinged_at >= first_pinged_at


# --- simulated write failure -------------------------------------------------


def test_a_commit_failure_is_reported_as_failing(
    settings: Settings, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate the database becoming unwritable (locked/read-only/broken) by
    making ``session.commit`` raise, without touching a real database file."""

    def _broken_commit() -> None:
        raise RuntimeError("simulated database write failure (COL-80 test)")

    monkeypatch.setattr(session, "commit", _broken_commit)

    results = run_database_writable_check(_context(settings, session))

    assert len(results) == 1
    result = results[0]
    assert result.code == DATABASE_UNWRITABLE_CODE
    assert result.category == DATABASE_WRITABLE_CATEGORY
    assert result.severity == SEVERITY_ERROR
    assert result.passing is False
    assert "simulated database write failure" in result.message


def test_a_failed_write_rolls_back_leaving_the_session_usable_afterwards(
    settings: Settings, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check must roll back on failure so the same session/tick can still
    be used afterwards (e.g. by :func:`~collapsarr.health.service.
    reconcile_health_results`), rather than leaving it stuck mid-transaction."""
    real_commit = session.commit
    calls = {"commit": 0}

    def _fail_once() -> None:
        calls["commit"] += 1
        if calls["commit"] == 1:
            raise RuntimeError("simulated database write failure (COL-80 test)")
        real_commit()

    monkeypatch.setattr(session, "commit", _fail_once)

    first = run_database_writable_check(_context(settings, session))
    assert first[0].passing is False

    # The session must still be usable for a subsequent, real operation.
    second = run_database_writable_check(_context(settings, session))
    assert second[0].passing is True


# --- registration -------------------------------------------------------------


def test_registered_with_default_health_checks() -> None:
    names = [check.name for check in default_health_checks()]

    assert DATABASE_WRITABLE_CHECK_NAME in names


def test_check_code_does_not_collide_with_other_col_74_codes() -> None:
    """CONTEXT.md: every new check must use a distinct `{SEVERITY}-{CATEGORY}-{SEQ}`
    Check Code -- assert this one doesn't collide with the existing checks'."""
    other_codes = {"WARN-ARR-001", "ERR-CONN-001", "WARN-DISK-001", "ERR-DISK-001"}

    assert DATABASE_UNWRITABLE_CODE not in other_codes
