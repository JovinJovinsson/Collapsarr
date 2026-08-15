"""recently processed window

Revision ID: b01929bfdfa4
Revises: 77625896d817
Create Date: 2026-08-11 16:45:00.000000

Additive migration for COL-167: a new ``recently_processed_window_minutes``
NOT NULL column on the singleton ``global_settings`` row (default 360 --
6h, matching ``scan_interval_hours``'s own default, which is what this field
replaces as the scheduler's "recently processed" dedup cooldown -- see
:mod:`collapsarr.jobs.scheduler`'s module docstring). Carries a
``server_default`` so an existing install's row is backfilled with the
documented default in the same ``ALTER TABLE`` rather than needing a
separate data migration, matching the treatment of ``backup_interval_days``/
``backup_retention_days`` (COL-66).
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b01929bfdfa4'
down_revision: str | None = '77625896d817'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'recently_processed_window_minutes',
                sa.Integer(),
                server_default=sa.text('360'),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.drop_column('recently_processed_window_minutes')
