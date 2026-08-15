"""job history kind

Revision ID: 65810a5d0e35
Revises: 8c7c1e39e948
Create Date: 2026-08-11 07:27:50.698468

Additive migration for COL-155: a ``kind`` column on ``job_history``,
distinguishing a ``downmix`` job run from a ``set_default_audio`` one (the
manual/bulk Default Audio Track fix). ``NOT NULL`` with ``server_default``
``'downmix'`` -- unlike the nullable Default Audio Track columns added in
COL-151/COL-154 (where ``NULL`` genuinely means "unset, no effect"), every
job-history row that existed before this column did was, definitionally, a
downmix job (``set_default_audio`` jobs didn't exist yet), so backfilling
them to ``'downmix'`` in the same ``ALTER TABLE`` is the correct value, not
just a placeholder -- old and new history reads consistently.

Indexed to match ``status``'s existing index -- both are small,
low-cardinality columns a future Activity/History view is likely to filter
by.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '65810a5d0e35'
down_revision: str | None = '8c7c1e39e948'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('job_history', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'kind',
                sa.Enum('downmix', 'set_default_audio', name='jobkind'),
                server_default='downmix',
                nullable=False,
            )
        )
        batch_op.create_index(batch_op.f('ix_job_history_kind'), ['kind'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('job_history', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_job_history_kind'))
        batch_op.drop_column('kind')
