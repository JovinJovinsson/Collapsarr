"""Service-layer persistence and querying for job history (COL-21).

Plain functions taking a SQLAlchemy :class:`~sqlalchemy.orm.Session` and a
:class:`~collapsarr.jobs.queue.Job`, matching the pattern already used by
:mod:`collapsarr.arr.service`. HTTP exposure (a future Activity/History
view) is a separate epic's concern -- this module is the whole surface.

:func:`record_job_history` is the write path: call it with a
:class:`~collapsarr.jobs.queue.Job` at any point in its lifecycle (right
after :meth:`~collapsarr.jobs.queue.JobQueue.enqueue`, and again after
:meth:`~collapsarr.jobs.queue.JobQueue.run_pending` completes it) to persist
its current state. It upserts by ``job_id`` so the same
:class:`~collapsarr.jobs.models.JobHistory` row is updated in place across
calls rather than duplicated.

:func:`list_job_history` and :func:`get_job_history` are the read path.

:func:`make_history_recorder` bridges this module to
:class:`~collapsarr.jobs.queue.JobQueue`'s ``history_recorder`` hook, so
every job run gets persisted automatically as it completes rather than
relying on a caller to remember to call :func:`record_job_history`. This
module (not :mod:`collapsarr.jobs.queue`, which cannot import this module
back without a cycle) owns that bridge.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import JobHistory
from .queue import HistoryRecorder, Job, JobStatus


def _exit_code(job: Job) -> int | None:
    """FFmpeg's exit code from the remux stage, or ``None`` if never reached."""
    if job.result is not None and job.result.remux_result is not None:
        return job.result.remux_result.returncode
    return None


def _error_text(job: Job) -> str | None:
    """The failure's human-readable text, or ``None`` for a non-failed job."""
    if job.error is not None:
        return str(job.error)
    if job.result is not None and not job.result.success:
        return job.result.detail
    return None


def _target(job: Job) -> str | None:
    """Comma-joined enabled downmix targets the job was configured with."""
    if not job.settings.enabled_targets:
        return None
    return ",".join(sorted(target.value for target in job.settings.enabled_targets))


def _language(job: Job) -> str | None:
    """Comma-joined language allow-list, or ``None`` when unrestricted."""
    if job.settings.language_allow_list is None:
        return None
    return ",".join(sorted(job.settings.language_allow_list))


def record_job_history(session: Session, job: Job) -> JobHistory:
    """Persist ``job``'s current state, creating or updating its history row.

    Looks up an existing :class:`JobHistory` row by ``str(job.id)``; if none
    exists yet, creates one. Every persisted field (status, started/ended
    timestamps, exit code, error text, target/language) is (re)computed from
    ``job``'s current state and written, so calling this again later (e.g.
    once a pending job has finished running) updates the same row rather
    than creating a duplicate.
    """
    job_id = str(job.id)
    history = session.scalars(select(JobHistory).where(JobHistory.job_id == job_id)).one_or_none()
    if history is None:
        history = JobHistory(job_id=job_id, file_path=str(job.file_path))
        session.add(history)

    history.file_path = str(job.file_path)
    history.status = job.status
    history.started_at = job.started_at
    history.ended_at = job.ended_at
    history.exit_code = _exit_code(job)
    history.error_text = _error_text(job)
    history.target = _target(job)
    history.language = _language(job)

    session.commit()
    session.refresh(history)
    return history


def get_job_history(session: Session, job_id: UUID | str) -> JobHistory | None:
    """Return the history row for ``job_id``, or ``None`` if none exists."""
    return session.scalars(
        select(JobHistory).where(JobHistory.job_id == str(job_id))
    ).one_or_none()


def list_job_history(
    session: Session,
    *,
    file_path: str | Path | None = None,
    status: JobStatus | None = None,
) -> list[JobHistory]:
    """Return persisted job history, optionally filtered by file or status.

    Ordered by ``id`` (insertion order), matching the pattern used
    throughout the service layer. ``file_path`` matches exactly (the same
    string form :func:`record_job_history` stores, i.e. ``str(job.file_path)``);
    ``status`` matches a single :class:`~collapsarr.jobs.queue.JobStatus`.
    Both filters may be combined; omitting both returns every row.
    """
    stmt = select(JobHistory).order_by(JobHistory.id)
    if file_path is not None:
        stmt = stmt.where(JobHistory.file_path == str(file_path))
    if status is not None:
        stmt = stmt.where(JobHistory.status == status)
    return list(session.scalars(stmt))


def make_history_recorder(session_factory: sessionmaker[Session]) -> HistoryRecorder:
    """Build a :class:`~collapsarr.jobs.queue.JobQueue`-compatible ``history_recorder``.

    The returned callable opens a **fresh** :class:`~sqlalchemy.orm.Session`
    from ``session_factory`` on every call and closes it again before
    returning -- never reusing or sharing one session across calls. That
    matters because :class:`~collapsarr.jobs.queue.JobQueue` invokes
    ``history_recorder`` from whichever worker thread just finished running
    the job (up to ``max_concurrency`` threads may call it concurrently);
    SQLAlchemy sessions are not safe to share across threads, but a
    ``sessionmaker`` is safe to call from any thread to obtain an
    independent one, which is exactly the pattern
    :func:`collapsarr.database.get_session` already uses per-request.

    Typical use::

        session_factory = create_session_factory(engine)
        queue = JobQueue.from_settings(
            history_recorder=make_history_recorder(session_factory)
        )
    """

    def recorder(job: Job) -> None:
        with session_factory() as session:
            record_job_history(session, job)

    return recorder
