"""current default audio track snapshot

Revision ID: 8c7c1e39e948
Revises: 7672e421e73a
Create Date: 2026-08-11 06:43:53.302617

Additive migration for COL-154: two new nullable columns on
``tracked_media_files``, mirroring how ``default_audio_language`` was added
to ``global_settings`` in COL-151 (no ``server_default`` -- ``NULL`` is the
correct backfilled value for an existing row, meaning "not yet probed since
this shipped", not a real "no default track" fact):

* ``current_default_language`` -- the language of the audio stream that
  currently carries the container's Default Audio Track disposition, as of
  the last probe (scan/webhook/manual trigger).
* ``current_default_channel_layout`` -- that same stream's channel layout
  (e.g. ``"5.1"``, ``"stereo"``).

Neither is indexed -- this is read-only display data for the Library page,
not a query filter.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8c7c1e39e948'
down_revision: str | None = '7672e421e73a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('tracked_media_files', schema=None) as batch_op:
        batch_op.add_column(sa.Column('current_default_language', sa.String(length=50), nullable=True))
        batch_op.add_column(
            sa.Column('current_default_channel_layout', sa.String(length=50), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('tracked_media_files', schema=None) as batch_op:
        batch_op.drop_column('current_default_channel_layout')
        batch_op.drop_column('current_default_language')
