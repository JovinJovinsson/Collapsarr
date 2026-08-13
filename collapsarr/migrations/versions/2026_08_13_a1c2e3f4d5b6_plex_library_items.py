"""plex library item mapping table

Revision ID: a1c2e3f4d5b6
Revises: f3a8b1c9d2e6
Create Date: 2026-08-13 14:00:00.000000

Additive migration for COL-210: a new ``plex_library_items`` table mapping a
tracked file's on-disk path to its Plex library item (``rating_key`` +
``section_key``). Rebuilt wholesale on every Plex Sync run
(``collapsarr.plex.library_sync.rebuild_library_items``) -- pure derived
state, no operator-owned columns, so a sync deletes and re-inserts every row.
The ``file_path`` column is unique+indexed, matching ``tracked_media_files``'s
own path key, so a file's ratingKey is a single indexed lookup by the path
Collapsarr already knows it by (``collapsarr.plex.models.PlexLibraryItem``).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1c2e3f4d5b6'
down_revision: str | None = 'f3a8b1c9d2e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'plex_library_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('file_path', sa.String(length=1000), nullable=False),
        sa.Column('rating_key', sa.String(length=100), nullable=False),
        sa.Column('section_key', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_plex_library_items_file_path'),
        'plex_library_items',
        ['file_path'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_plex_library_items_file_path'), table_name='plex_library_items')
    op.drop_table('plex_library_items')
