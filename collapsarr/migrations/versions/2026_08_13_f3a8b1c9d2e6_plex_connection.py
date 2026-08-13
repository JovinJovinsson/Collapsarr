"""plex connection singleton config

Revision ID: f3a8b1c9d2e6
Revises: 4747249cb199
Create Date: 2026-08-13 12:00:00.000000

Additive migration for COL-209: a new singleton ``plex_connection`` table
storing the operator's Plex Media Server connection (``base_url`` +
``X-Plex-Token``). Mirrors ``notifier_config``'s singleton-row convention
(``CheckConstraint('id = 1', ...)``) and ``arr_instances``'s cached
connectivity columns (``status``/``status_error``/``status_checked_at``/
``version``). The status enum is given its own DB-level name
(``plex_connectivity_status``) distinct from ``arr_instances``'s
``connectivitystatus`` so the two stay independent at the schema level, even
though both currently share the same three string values -- see
``collapsarr.plex.models.ConnectivityStatus``'s docstring. ``token`` is a
server-side-only secret -- see ``collapsarr.plex.routes`` for how it is kept
out of every HTTP response.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f3a8b1c9d2e6'
down_revision: str | None = '4747249cb199'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'plex_connection',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('base_url', sa.String(length=500), nullable=False),
        sa.Column('token', sa.String(length=255), nullable=False),
        sa.Column(
            'status',
            sa.Enum('unknown', 'ok', 'error', name='plex_connectivity_status'),
            nullable=False,
        ),
        sa.Column('status_error', sa.Text(), nullable=True),
        sa.Column('status_checked_at', sa.DateTime(), nullable=True),
        sa.Column('version', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('id = 1', name='ck_plex_connection_singleton'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('plex_connection')
