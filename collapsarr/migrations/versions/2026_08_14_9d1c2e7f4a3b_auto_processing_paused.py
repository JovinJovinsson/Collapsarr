"""auto processing paused

Revision ID: 9d1c2e7f4a3b
Revises: c4d5e6f7a8b9
Create Date: 2026-08-14 08:20:00.000000

Additive migration for COL-226 ("Auto-Processing Pause"): a new
``auto_processing_paused`` NOT NULL boolean column on the singleton
``global_settings`` row (default ``False`` -- job processing stays on unless
an operator explicitly pauses it). Carries a ``server_default`` so an
existing install's row is backfilled to ``False`` in the same ``ALTER
TABLE`` rather than needing a separate data migration, matching the
treatment of ``auto_queue_paused`` (COL-174).
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d1c2e7f4a3b'
down_revision: str | None = 'c4d5e6f7a8b9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'auto_processing_paused',
                sa.Boolean(),
                server_default=sa.text('0'),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.drop_column('auto_processing_paused')
