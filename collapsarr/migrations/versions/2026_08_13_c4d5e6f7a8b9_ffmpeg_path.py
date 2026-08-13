"""ffmpeg path

Revision ID: c4d5e6f7a8b9
Revises: a1c2e3f4d5b6
Create Date: 2026-08-13 12:00:00.000000

Additive migration for COL-218: an optional ``ffmpeg_path`` override on the
singleton ``global_settings`` row.

* ``ffmpeg_path`` -- nullable, no ``server_default``. ``NULL`` (unset) is the
  correct backfilled value for an existing install's row -- it preserves
  today's behaviour of resolving the bare ``"ffmpeg"`` command off ``PATH``,
  same treatment as ``default_audio_language``/``log_level``.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: str | None = 'a1c2e3f4d5b6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ffmpeg_path', sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.drop_column('ffmpeg_path')
