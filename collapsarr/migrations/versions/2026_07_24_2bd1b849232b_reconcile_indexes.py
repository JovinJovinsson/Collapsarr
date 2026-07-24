"""reconcile indexes

Revision ID: 2bd1b849232b
Revises: afde30c41b7b
Create Date: 2026-07-24 22:25:06.564593

The heal step for databases adopted at baseline (COL-59). A ``create_all``-era
install has no ``alembic_version``; the startup routine stamps it at the
baseline revision, then runs this delta. Fresh installs already have every
index from the baseline migration, so each statement here is a guarded no-op
(``CREATE INDEX IF NOT EXISTS`` / ``CREATE UNIQUE INDEX IF NOT EXISTS``).

Why this exists: the retired COL-48 ``ensure_schema`` could only *add columns*,
never indexes, so a DB that was create_all'd at an older release and then
column-backfilled may be missing indexes the baseline declares. Recreating them
idempotently closes that gap without touching data. Every index below mirrors
the baseline definition exactly (name, columns, uniqueness), so re-declaring an
already-present one is a true no-op.

Table-level ``UNIQUE``/``FOREIGN KEY`` constraints (e.g.
``uq_arr_instances_name``) are baked into ``CREATE TABLE`` and cannot be
independently missing from an adopted DB, so they are out of scope here — only
the SQLAlchemy-declared indexes are reconciled.

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2bd1b849232b'
down_revision: str | None = 'afde30c41b7b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (index name, table, column expression, unique?) — mirrors the baseline exactly.
_INDEXES: tuple[tuple[str, str, str, bool], ...] = (
    ("ix_arr_instances_name", "arr_instances", "name", False),
    ("ix_arr_instances_type", "arr_instances", "type", False),
    ("ix_job_history_file_path", "job_history", "file_path", False),
    ("ix_job_history_job_id", "job_history", "job_id", True),
    ("ix_job_history_status", "job_history", "status", False),
    ("ix_tracked_media_files_file_path", "tracked_media_files", "file_path", True),
    ("ix_remote_path_mappings_instance_id", "remote_path_mappings", "instance_id", False),
    ("ix_tracked_media_target_status_media_id", "tracked_media_target_status", "media_id", False),
    ("ix_tracked_media_target_status_status", "tracked_media_target_status", "status", False),
)


def upgrade() -> None:
    # Idempotent: IF NOT EXISTS means a fresh install (indexes already built by
    # the baseline) is a no-op, while an adopted DB gains any missing indexes.
    for name, table, column, unique in _INDEXES:
        kind = "UNIQUE INDEX" if unique else "INDEX"
        op.execute(
            f'CREATE {kind} IF NOT EXISTS "{name}" ON "{table}" ("{column}")'
        )


def downgrade() -> None:
    # Intentionally a no-op. This migration only *ensures* baseline-owned
    # indexes exist idempotently -- it does not own them, so it cannot know
    # which (if any) it actually created versus found already present. The
    # baseline migration's downgrade is what drops these indexes; dropping them
    # here too would double-drop (and error) on a full downgrade to base.
    pass
