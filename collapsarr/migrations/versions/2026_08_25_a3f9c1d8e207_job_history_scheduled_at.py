"""job history scheduled at

Revision ID: a3f9c1d8e207
Revises: 42f6e2c6c70b
Create Date: 2026-08-25 00:00:00.000000

Additive migration for COL-242 ("Scheduled Job status + due-time gate"): one
new nullable ``scheduled_at`` column on ``job_history``, mirroring
:attr:`~collapsarr.jobs.queue.Job.scheduled_at`. ``NULL`` (the backfill value
for every pre-existing row, and the default for every Job kind/trigger that
exists today) means "claimable as soon as a worker is free" -- unchanged
behaviour. A non-``None`` value gates when the worker pool's claim path
(``JobQueue._claim_next``) may claim an otherwise-``pending`` Job. No
``server_default`` needed -- every existing row is simply "not scheduled"
(``NULL``), which is exactly what an unspecified nullable column backfills
to. New column only -- no data migration, and no new ``JobStatus`` value.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f9c1d8e207'
down_revision: str | None = '42f6e2c6c70b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('job_history', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scheduled_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('job_history', schema=None) as batch_op:
        batch_op.drop_column('scheduled_at')
