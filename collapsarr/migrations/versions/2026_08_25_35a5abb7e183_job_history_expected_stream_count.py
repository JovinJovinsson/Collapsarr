"""job history expected stream count

Revision ID: 35a5abb7e183
Revises: d2aa7dc1a72d
Create Date: 2026-08-25 00:00:00.000000

Additive migration for COL-251 ("Downmix pipeline enqueues delayed Default
Audio Track Job + hard-fail on missing stream"): one new nullable
``expected_stream_count`` column on ``job_history``, mirroring
:attr:`~collapsarr.jobs.queue.Job.expected_stream_count`. ``NULL`` (the
backfill value for every pre-existing row, and the default for every
immediate trigger -- manual/bulk, plain webhook) means "no ingestion-wait
check" -- unchanged behaviour. A non-``None`` value, set only for a
downmix-triggered ``SET_DEFAULT_AUDIO`` Job, is the file's total audio-stream
count as of the downmix remux that scheduled it; the direct Plex API write
mechanism (``apply_default_audio_via_plex``) uses it to fail distinctly
(``PlexDefaultAudioOutcome.STREAM_NOT_YET_INGESTED``) when Plex still reports
fewer streams than that. Persisted (mirroring ``scheduled_at``, COL-242) so a
downmix-triggered row's ingestion-wait safety check survives a restart while
still ``pending`` and not yet due -- see
:func:`~collapsarr.jobs.rehydrate._reconstruct_job`. No ``server_default``
needed -- every existing row correctly backfills to ``NULL``. New column
only -- no data migration, and no new ``JobStatus`` value.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '35a5abb7e183'
down_revision: str | None = 'd2aa7dc1a72d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('job_history', schema=None) as batch_op:
        batch_op.add_column(sa.Column('expected_stream_count', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('job_history', schema=None) as batch_op:
        batch_op.drop_column('expected_stream_count')
