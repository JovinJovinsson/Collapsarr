"""Downmix-job-completion -> tracked-media bridge (COL-95).

:mod:`collapsarr.jobs.queue` (COL-20) captures the outcome of every job run
onto its own in-memory :class:`~collapsarr.jobs.queue.Job`. This module is
the bridge from that outcome to :mod:`collapsarr.media.service`'s tracked-media
write path (COL-25): when a job reaches ``SUCCEEDED``, :func:`record_tracked_media`
flips every ``(language, target)`` pair the pipeline actually added
(:attr:`~collapsarr.downmix.pipeline.PipelineResult.tracks_added`) to
``PROCESSED`` via :func:`~collapsarr.media.service.record_target_processed`,
so a file's now-satisfied target stops appearing in the Wanted view
immediately -- without waiting for the next scan to re-probe it.

Mirrors :mod:`collapsarr.jobs.history`'s and :mod:`collapsarr.jobs.
failure_notify`'s bridge pattern: a plain function taking a SQLAlchemy
:class:`~sqlalchemy.orm.Session` and a :class:`~collapsarr.jobs.queue.Job`,
plus a ``session_factory``-bound constructor (:func:`make_tracked_media_recorder`)
matching :class:`~collapsarr.jobs.queue.JobQueue`'s ``tracked_media_recorder``
hook signature.

Reusing ``tracks_added`` (rather than re-probing the file here) is
deliberate -- the pipeline already knows exactly which pairs it added, so
this needs no new probe call and can't disagree with what the pipeline
itself reports. :func:`~collapsarr.media.service.record_target_processed`'s
own docstring notes the two write paths (this one, and a later scan calling
:func:`~collapsarr.media.service.upsert_tracked_media`) independently arrive
at the same ``PROCESSED`` state, so they never disagree either.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from collapsarr.media.service import record_target_processed

from .queue import Job, JobStatus, TrackedMediaRecorder


def record_tracked_media(session: Session, job: Job) -> None:
    """Flip ``job``'s actually-processed ``(language, target)`` pairs to ``PROCESSED``.

    A no-op unless ``job`` reached :attr:`~collapsarr.jobs.queue.JobStatus.SUCCEEDED`
    with a pipeline result -- a job that failed, or one whose result reported
    :attr:`~collapsarr.downmix.pipeline.PipelineOutcome.NOTHING_TO_DO` (an
    empty ``tracks_added``), has nothing new to record.
    """
    if job.status is not JobStatus.SUCCEEDED or job.result is None:
        return
    for qualifying in job.result.tracks_added:
        record_target_processed(
            session,
            file_path=job.file_path,
            language=qualifying.language,
            target=qualifying.target,
        )


def make_tracked_media_recorder(session_factory: sessionmaker[Session]) -> TrackedMediaRecorder:
    """Build a :class:`~collapsarr.jobs.queue.JobQueue`-compatible ``tracked_media_recorder``.

    The returned callable opens a **fresh** :class:`~sqlalchemy.orm.Session`
    from ``session_factory`` on every call and closes it again before
    returning -- the same convention :func:`collapsarr.jobs.history.
    make_history_recorder` and :func:`collapsarr.jobs.failure_notify.
    make_failure_notifier` use, and for the same reason: :class:`~collapsarr.
    jobs.queue.JobQueue` may invoke this from any of up to ``max_concurrency``
    worker threads, and SQLAlchemy sessions aren't safe to share across
    threads.

    Typical use::

        session_factory = create_session_factory(engine)
        queue = JobQueue.from_settings(
            tracked_media_recorder=make_tracked_media_recorder(session_factory)
        )
    """

    def recorder(job: Job) -> None:
        with session_factory() as session:
            record_tracked_media(session, job)

    return recorder
