"""auto queue paused

Revision ID: 4747249cb199
Revises: b01929bfdfa4
Create Date: 2026-08-11 18:15:00.000000

Additive migration for COL-174 ("Auto-Queuing Pause"): a new
``auto_queue_paused`` NOT NULL boolean column on the singleton
``global_settings`` row (default ``False`` -- auto-fill stays on unless an
operator explicitly pauses it). Carries a ``server_default`` so an existing
install's row is backfilled to ``False`` in the same ``ALTER TABLE`` rather
than needing a separate data migration, matching the treatment of
``auto_set_default_audio`` (COL-151) / ``recently_processed_window_minutes``
(COL-167).
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4747249cb199'
down_revision: str | None = 'b01929bfdfa4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'auto_queue_paused',
                sa.Boolean(),
                server_default=sa.text('0'),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.drop_column('auto_queue_paused')
