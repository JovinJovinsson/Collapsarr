"""log level

Revision ID: 8b2ebda7b5ca
Revises: c6e21d7b6185
Create Date: 2026-08-09 12:00:00.000000

Additive migration for COL-130: one new nullable column on the singleton
``global_settings`` row -- ``log_level`` (``DEBUG``|``INFO``|``WARNING``|
``ERROR``, or ``NULL``). No ``server_default`` -- unlike ``update_channel``'s
``NOT NULL``-with-backfill treatment, ``NULL`` here is itself the correct
backfilled value for an existing install's row: it means "no override, keep
resolving the level from the ``COLLAPSARR_LOG_LEVEL`` environment setting at
boot", exactly the behaviour that row already had before this column existed.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b2ebda7b5ca'
down_revision: str | None = 'c6e21d7b6185'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('log_level', sa.String(length=10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.drop_column('log_level')
