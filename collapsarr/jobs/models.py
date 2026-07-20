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
(the job is enqueued and has not started running yet).

:mod:`collapsarr.jobs.history` is the service layer (record/list/get) built
on top of this model; nothing in this module touches a session.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base
from collapsarr.jobs.queue import JobStatus


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
    the job's :class:`~collapsarr.downmix.targets.DownmixSettings` -- so
    they're always present regardless of which stage the run reached
    (unlike the pipeline's ``tracks_added``, which is only populated on
    success). ``language`` is ``None`` when the job had no allow-list
    (evaluates every language present on the file).
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
