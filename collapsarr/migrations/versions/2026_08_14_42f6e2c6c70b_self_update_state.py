"""self update state

Revision ID: 42f6e2c6c70b
Revises: 9d1c2e7f4a3b
Create Date: 2026-08-14 12:00:00.000000

Schema for COL-230 ("Self-update state, in-progress guard, checksum client,
status endpoint", foundational slice of Epic COL-224). Two changes, bundled
into a single migration since both land together for the same ticket and
both are purely additive:

* A new singleton ``self_update_state`` table (mirrors ``update_check_state``/
  ``plex_connection``'s ``CheckConstraint('id = 1', ...)`` singleton-row
  idiom) -- ``in_progress`` (the guard), ``phase``, and ``previous_version``
  (the rollback pointer). A fresh ``create_table``, so no ``server_default``s
  are needed the way an ``ALTER TABLE`` on an existing row would require --
  there are no pre-existing rows to backfill.
* A new nullable ``auto_processing_pause_restore_value`` boolean column on
  the existing singleton ``global_settings`` row -- scratch space for the
  self-update apply flow's one-shot pause-restore mechanism (COL-233). No
  ``server_default``: ``NULL`` (no in-flight self-update) is the correct
  backfilled value for an existing install's row, same treatment as
  ``ffmpeg_path``/``log_level``.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '42f6e2c6c70b'
down_revision: str | None = '9d1c2e7f4a3b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'self_update_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('in_progress', sa.Boolean(), nullable=False),
        sa.Column('phase', sa.String(length=20), nullable=False),
        sa.Column('previous_version', sa.String(length=100), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint('id = 1', name='ck_self_update_state_singleton'),
        sa.PrimaryKeyConstraint('id'),
    )

    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('auto_processing_pause_restore_value', sa.Boolean(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.drop_column('auto_processing_pause_restore_value')

    op.drop_table('self_update_state')
