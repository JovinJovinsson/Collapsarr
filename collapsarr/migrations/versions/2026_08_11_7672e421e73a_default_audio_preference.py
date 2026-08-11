"""default audio preference

Revision ID: 7672e421e73a
Revises: 8b2ebda7b5ca
Create Date: 2026-08-11 09:00:00.000000

Additive migration for COL-151: the Preferred Default Audio setting. Three
new columns on the singleton ``global_settings`` row:

* ``default_audio_language`` -- nullable, no ``server_default``. ``NULL``
  (unset) is the correct backfilled value for an existing install's row --
  there is no sensible universal default language, so the feature simply has
  no effect until an operator configures it, same treatment as
  ``log_level``.
* ``default_audio_channel_tier`` -- nullable, no ``server_default``, same
  reasoning as ``default_audio_language`` above.
* ``auto_set_default_audio`` -- ``NOT NULL`` boolean, ``server_default``
  ``'0'`` (false) so an existing install's row is backfilled to off in the
  same ``ALTER TABLE`` -- matching ``default_tracked``'s additive treatment.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7672e421e73a'
down_revision: str | None = '8b2ebda7b5ca'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('default_audio_language', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('default_audio_channel_tier', sa.String(length=10), nullable=True))
        batch_op.add_column(
            sa.Column(
                'auto_set_default_audio',
                sa.Boolean(),
                server_default=sa.text('0'),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.drop_column('auto_set_default_audio')
        batch_op.drop_column('default_audio_channel_tier')
        batch_op.drop_column('default_audio_language')
