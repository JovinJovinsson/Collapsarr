"""ORM model for persisted job run history (COL-21).

:mod:`collapsarr.jobs.queue` (COL-20) runs each enqueued file through the
downmix pipeline and captures the outcome onto its own in-memory
:class:`~collapsarr.jobs.queue.Job` -- ``status``, ``result``/``error``, and
(as of COL-21) ``started_at``/``ended_at``. That in-memory state doesn't
survive a process restart and isn't queryable, which is what this module
fixes: :class:`JobHistory` is the durable row a completed (or in-flight)
:class:`~collapsarr.jobs.queue.Job` gets persisted into, ready for a future
Activity/History view.

Deliberately reuses :class:`~collapsarr.jobs.queue.JobStatus` for the
``status`` column rather than inventing a parallel status vocabulary --
``JobStatus.PENDING`` is this ticket's "queued" per the acceptance criteria
(the job is enqueued and has not started running yet). Likewise reuses
:class:`~collapsarr.jobs.queue.JobKind` (COL-155) for the ``kind`` column --
``DOWNMIX`` or ``SET_DEFAULT_AUDIO`` -- rather than a parallel vocabulary.

:mod:`collapsarr.jobs.history` is the service layer (record/list/get) built
on top of this model; nothing in this module touches a session.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base
from collapsarr.jobs.queue import JobKind, JobStatus


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobHistory(Base):
    """One persisted record of a single job run.

    ``job_id`` is the string form of the originating
    :attr:`~collapsarr.jobs.queue.Job.id` and is unique -- :func:`collapsarr.
    jobs.history.record_job_history` upserts by it, so the same row is
    updated in place as a job progresses from queued -> running ->
    succeeded/failed rather than accumulating one row per state change.

    ``exit_code`` is FFmpeg's exit code from the remux stage
    (:attr:`~collapsarr.downmix.remux.RemuxResult.returncode`) when the
    pipeline reached that stage, else ``None`` (e.g. a probe failure, or
    "nothing to do"). ``error_text`` is populated for a failed run: either
    the unexpected exception's message, or the pipeline result's ``detail``
    when the pipeline itself reported a failure outcome.

    ``target``/``language`` capture what the job was configured to do --
    the comma-joined enabled downmix targets and language allow-list from
    the job's :class:`~collapsarr.downmix.targets.DownmixSettings` for a
    ``DOWNMIX`` job, or the resolved channel tier/language from the job's
    :class:`~collapsarr.downmix.default_audio.DefaultAudioPreference` for a
    ``SET_DEFAULT_AUDIO`` job (COL-155; see
    :func:`~collapsarr.jobs.history.record_job_history`) -- so they're always
    present regardless of which stage the run reached (unlike the pipeline's
    ``tracks_added``, which is only populated on success). ``language`` is
    ``None`` when a ``DOWNMIX`` job had no allow-list (evaluates every
    language present on the file).

    ``kind`` (COL-155) is ``DOWNMIX`` by default -- both for a freshly
    created row and, via the migration that added this column, for every
    pre-existing row, so old and new history reads consistently as "this was
    a downmix job" without a manual backfill step.

    ``priority`` (COL-163) mirrors the originating
    :attr:`~collapsarr.jobs.queue.Job.priority` -- the join-order sequence
    number :meth:`~collapsarr.jobs.queue.JobQueue._enqueue` assigns; its
    Python-side ``default=0`` (like ``status``'s above) only matters for a
    row built without going through :func:`~collapsarr.jobs.history.
    record_job_history` (e.g. a test seeding a row directly) -- every real
    job's row gets its actual priority explicitly. Its migration backfills
    every pre-existing row from its own ``id`` (this table's insertion
    order), so an upgrade doesn't scramble whatever ordering was already
    implied by existing history. This is a pure prefactor -- nothing yet
    reads this column back to change execution order (that is COL-164's
    priority-pull worker pool).
    """

    __tablename__ = "job_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False, index=True)
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(
            JobStatus,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=JobStatus.PENDING,
        index=True,
    )
    kind: Mapped[JobKind] = mapped_column(
        SAEnum(
            JobKind,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=JobKind.DOWNMIX,
        # DB-side server_default (matching `GlobalSettings.auto_set_default_audio`'s
        # treatment) so the COL-155 migration can backfill every pre-existing row to
        # `'downmix'` in the same additive `ALTER TABLE`, not just new rows going
        # forward -- see the migration's own docstring for why that backfill value
        # (rather than nullable/unset) is correct here.
        server_default=text("'downmix'"),
        index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    target: Mapped[str | None] = mapped_column(String(100), nullable=True, default=None)
    language: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return (
            f"JobHistory(id={self.id!r}, job_id={self.job_id!r}, "
            f"file_path={self.file_path!r}, status={self.status!r})"
        )
