"""Tests for the ``job_history.priority`` migration (COL-163).

Mirrors ``test_migration_log_level.py``'s "post-baseline delta" idiom: build
a database at the migration immediately prior to this one, insert real
pre-migration ``job_history`` rows (no ``priority`` column exists yet), then
upgrade and assert ``priority`` is backfilled from each row's own ``id`` --
i.e. its insertion order -- rather than left ``NULL`` or scrambled.
"""

from __future__ import annotations

from alembic import command
from sqlalchemy import inspect, text

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings
from collapsarr.migrations import build_alembic_config, upgrade_to_head

#: The revision immediately prior to COL-163's migration (COL-155's
#: ``job_history.kind`` column).
_PRIOR_REVISION = "65810a5d0e35"


def _insert_pre_migration_row(job_id: str) -> str:
    return (
        "INSERT INTO job_history "
        "(job_id, file_path, status, created_at, updated_at) "
        f"VALUES ('{job_id}', '/media/{job_id}.mkv', 'pending', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )


def _create_pre_migration_job_history_rows(settings: Settings) -> None:
    """Stamp the DB at :data:`_PRIOR_REVISION` and insert three real existing rows.

    Models an already-deployed install about to take the COL-163 update: its
    ``job_history`` rows predate the ``priority`` column entirely, and were
    inserted in this order (so ``id`` 1, 2, 3 -- SQLite's autoincrement rowid
    -- reflects that insertion order, same as production).
    """
    config = build_alembic_config(settings)
    command.upgrade(config, _PRIOR_REVISION)

    engine = create_engine_from_settings(settings)
    try:
        with engine.begin() as connection:
            for job_id in ("job-a", "job-b", "job-c"):
                connection.execute(text(_insert_pre_migration_row(job_id)))
    finally:
        engine.dispose()


def test_migration_backfills_priority_from_insertion_order(settings: Settings) -> None:
    """Existing rows get ``priority`` equal to their own ``id`` (insertion order)."""
    _create_pre_migration_job_history_rows(settings)

    upgrade_to_head(settings)

    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT job_id, id, priority FROM job_history ORDER BY id")
            ).all()
    finally:
        engine.dispose()

    assert [row.job_id for row in rows] == ["job-a", "job-b", "job-c"]
    # Backfilled from `id` -- ascending, matching original insertion order --
    # and not scrambled or left NULL.
    for row in rows:
        assert row.priority == row.id


def test_fresh_install_has_priority_column_not_nullable_and_indexed(settings: Settings) -> None:
    """A brand-new install's schema has ``priority``, NOT NULL and indexed."""
    upgrade_to_head(settings)

    engine = create_engine_from_settings(settings)
    try:
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("job_history")}
        assert "priority" in columns
        assert columns["priority"]["nullable"] is False

        index_names = {ix["name"] for ix in inspector.get_indexes("job_history")}
        assert "ix_job_history_priority" in index_names
    finally:
        engine.dispose()
