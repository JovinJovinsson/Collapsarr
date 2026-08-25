"""default audio delay minutes

Revision ID: d2aa7dc1a72d
Revises: 42f6e2c6c70b
Create Date: 2026-08-25 00:00:00.000000

Additive migration for COL-243: a new ``default_audio_delay_minutes`` NOT
NULL column on the singleton ``global_settings`` row (default 30 minutes),
modeled directly on ``recently_processed_window_minutes`` (COL-167, see
``2026_08_11_b01929bfdfa4_recently_processed_window.py``). Carries a
``server_default`` so an existing install's row is backfilled with the
documented default in the same ``ALTER TABLE`` rather than needing a
separate data migration. Not yet consumed by any Job-scheduling logic --
this ticket only adds the knob and its Settings UI field; COL-251 is the
follow-up ticket that reads it.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2aa7dc1a72d'
down_revision: str | None = '42f6e2c6c70b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'default_audio_delay_minutes',
                sa.Integer(),
                server_default=sa.text('30'),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.drop_column('default_audio_delay_minutes')
