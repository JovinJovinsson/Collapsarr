"""job history priority

Revision ID: 77625896d817
Revises: 65810a5d0e35
Create Date: 2026-08-11 15:30:00.000000

Additive migration for COL-163: a persisted ``priority`` column on
``job_history``, so job ordering survives a process restart and is available
for the priority-pull worker pool (COL-164, currently blocked on this ticket)
to build on. This is a pure prefactor -- nothing yet reads the column back to
change execution order; the worker pool (:meth:`~collapsarr.jobs.queue.JobQueue.start`
and its ``_worker_loop`` claim path) is untouched.

Three-phase, unlike the single-shot ``server_default`` used by the COL-155
``kind`` migration: ``priority`` needs a **per-row** backfill value (its
existing insertion order), not one constant valid for every prior row, so it
cannot be populated via a static ``server_default`` at ``ADD COLUMN`` time.

1. Add ``priority`` nullable (a plain ``ALTER TABLE ADD COLUMN`` -- no table
   rebuild needed yet).
2. Backfill every existing row from its own ``id`` -- ``job_history.id`` is
   an autoincrement primary key, i.e. exactly the table's insertion order,
   the same signal :func:`~collapsarr.jobs.history.list_job_history` already
   orders by. Backfilling from it means an upgrade preserves today's implied
   pending-queue order rather than scrambling it.
3. Tighten ``priority`` to ``NOT NULL`` and index it (SQLite can't ``ALTER
   COLUMN`` in place, so this phase needs a full batch table rebuild --
   split out from phase 1's plain add so that step stays a cheap no-rebuild
   operation).
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '77625896d817'
down_revision: str | None = '65810a5d0e35'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('job_history', schema=None) as batch_op:
        batch_op.add_column(sa.Column('priority', sa.Integer(), nullable=True))

    # Backfill from insertion order (`id`) so an upgrade doesn't scramble
    # today's pending queue.
    op.execute('UPDATE job_history SET priority = id')

    with op.batch_alter_table('job_history', schema=None, recreate='always') as batch_op:
        batch_op.alter_column('priority', existing_type=sa.Integer(), nullable=False)
        batch_op.create_index(batch_op.f('ix_job_history_priority'), ['priority'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('job_history', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_job_history_priority'))
        batch_op.drop_column('priority')
