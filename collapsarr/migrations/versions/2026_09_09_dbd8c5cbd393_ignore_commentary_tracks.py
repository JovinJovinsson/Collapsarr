"""ignore commentary tracks

Revision ID: dbd8c5cbd393
Revises: 35a5abb7e183
Create Date: 2026-09-09 00:00:00.000000

Additive migration for COL-244: a new ``ignore_commentary_tracks`` NOT NULL
column on the singleton ``global_settings`` row (default ``True``), modeled
directly on ``default_tracked`` (COL-98, see
``2026_08_04_d3f7a1b9c2e5_library_nodes_and_default_tracked.py``). Carries a
``server_default`` so an existing install's row is backfilled with the
documented default in the same ``ALTER TABLE`` rather than needing a
separate data migration. Not yet consumed by any detection/eligibility/
resolution logic -- this ticket only adds the knob and its Settings UI
field; COL-249/COL-250 are the follow-up tickets that read it.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dbd8c5cbd393'
down_revision: str | None = '35a5abb7e183'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'ignore_commentary_tracks',
                sa.Boolean(),
                server_default=sa.text('1'),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.drop_column('ignore_commentary_tracks')
