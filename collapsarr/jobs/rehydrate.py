"""Restart-durable queue rehydration (COL-166).

:class:`~collapsarr.jobs.queue.JobQueue` keeps its live work in a plain,
process-local ``dict`` (``_jobs``) -- gone the instant the process restarts.
Before this module existed, that meant every still-``PENDING``
:class:`~collapsarr.jobs.models.JobHistory` row written before a restart
survived on disk (a durable row) with nothing left to ever run it: a
permanent "ghost pending row," visible in history forever but never
picked up again.

:func:`rehydrate_pending_jobs` is the fix, meant to be called once, at
process startup, right after :meth:`~collapsarr.jobs.queue.JobQueue.
from_settings` builds the queue and before :meth:`~collapsarr.jobs.queue.
JobQueue.start` spins up its worker pool (see :mod:`collapsarr.main`). It:

1. Seeds :attr:`~collapsarr.jobs.queue.JobQueue._next_priority` via
   :meth:`~collapsarr.jobs.queue.JobQueue.seed_next_priority` -- see that
   method's docstring for exactly why (in short: a fresh post-restart
   enqueue must never collide with, or sort ahead of, a rehydrated Job's
   priority).
2. Reads every ``PENDING`` row, ordered by persisted ``priority``, and
   reconstructs each as a live :class:`~collapsarr.jobs.queue.Job` via
   :func:`_reconstruct_job` -- whose settings/preference are re-derived
   fresh from the *current* :class:`~collapsarr.settings.models.
   GlobalSettings` row, never replayed from the row's stored
   ``target``/``language`` summary (global downmix config, or the
   Preferred Default Audio preference, may have changed since the row was
   originally written).
3. Pushes the reconstructed Jobs onto the queue in that same priority
   order via :meth:`~collapsarr.jobs.queue.JobQueue.rehydrate`, which
   trusts each Job's ``priority`` as given rather than renumbering it.

This module (not :mod:`collapsarr.jobs.queue`) owns the DB read side of
rehydration, mirroring :mod:`collapsarr.jobs.history`/:mod:`collapsarr.jobs.
failure_notify`/:mod:`collapsarr.jobs.tracked_media`: :mod:`collapsarr.jobs.
queue` deliberately does not import any of these modules (they import *it*,
for :class:`~collapsarr.jobs.queue.Job`/:class:`~collapsarr.jobs.queue.
JobStatus`/:class:`~collapsarr.jobs.queue.JobKind` -- importing them back
would be circular), so ``JobQueue`` only exposes the plain primitives
(:meth:`~collapsarr.jobs.queue.JobQueue.seed_next_priority`,
:meth:`~collapsarr.jobs.queue.JobQueue.rehydrate`) this module drives from
the outside.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from collapsarr.downmix.targets import DownmixSettings
from collapsarr.settings.models import GlobalSettings
from collapsarr.settings.service import (
    as_default_audio_preference,
    as_downmix_settings,
    get_global_settings,
)

from .history import list_job_history
from .models import JobHistory
from .queue import Job, JobKind, JobQueue, JobStatus

logger = logging.getLogger(__name__)


def rehydrate_pending_jobs(session: Session, queue: JobQueue) -> list[Job]:
    """Reconstruct every ``PENDING`` ``JobHistory`` row onto ``queue`` (COL-166).

    Meant to be called once per process, at startup, after ``queue`` is
    built (e.g. via :meth:`~collapsarr.jobs.queue.JobQueue.from_settings`)
    but before :meth:`~collapsarr.jobs.queue.JobQueue.start` -- see
    :mod:`collapsarr.main`. Returns the list of Jobs actually rehydrated
    (in the same priority order they were pushed onto ``queue``), mainly
    for logging/testing; callers don't need to do anything further with it,
    since :meth:`~collapsarr.jobs.queue.JobQueue.rehydrate` has already
    inserted them.

    Always seeds ``queue``'s ``_next_priority`` counter first (see the
    module docstring) -- even when there is nothing ``PENDING`` to
    rehydrate, since a completed/failed row can still carry a priority
    higher than any pending one, and *that* value is what a fresh
    post-restart enqueue must clear.

    A ``PENDING`` row whose settings can no longer be resolved into a
    runnable Job (see :func:`_reconstruct_job` -- a malformed ``job_id``, an
    unrecognised ``kind``, or a ``SET_DEFAULT_AUDIO`` row whose preference is
    no longer configured) is flipped to ``FAILED`` (:func:`_mark_unrehydratable`)
    rather than left ``PENDING`` -- logged, not raised, so one bad row can't
    abort rehydrating every other one. Leaving it ``PENDING`` would recreate
    the exact permanent "ghost pending row" this module exists to eliminate:
    every future restart would re-attempt it, fail to resolve it the same
    way, and skip it again, forever, with nothing ever surfacing that it's
    stuck. ``FAILED`` (with ``error_text`` explaining why) is a normal,
    visible terminal state instead -- surfaced by the Activity/History view
    like any other failed job, and a user who still wants it done can
    manually retrigger it.
    """
    all_rows = list_job_history(session)
    if all_rows:
        queue.seed_next_priority(max(row.priority for row in all_rows) + 1)

    pending_rows = sorted(
        (row for row in all_rows if row.status is JobStatus.PENDING),
        key=lambda row: row.priority,
    )
    if not pending_rows:
        return []

    global_settings = get_global_settings(session)
    jobs: list[Job] = []
    for row in pending_rows:
        job, unrehydratable_reason = _reconstruct_job(row, global_settings)
        if job is None:
            _mark_unrehydratable(session, row, unrehydratable_reason or "cannot rehydrate")
            continue
        jobs.append(job)

    queue.rehydrate(jobs)
    for job in jobs:
        logger.info(
            "rehydrated job %s: kind=%s file=%s priority=%s",
            job.id,
            job.kind.value,
            job.file_path,
            job.priority,
        )
    return jobs


def _reconstruct_job(
    row: JobHistory, global_settings: GlobalSettings
) -> tuple[Job | None, str | None]:
    """Rebuild one ``PENDING`` ``JobHistory`` row as a live, still-``PENDING`` Job.

    ``settings``/``preference`` are re-derived fresh from ``global_settings``
    -- the *current* persisted row, read once by the caller for the whole
    rehydration pass -- rather than replayed from ``row.target``/
    ``row.language`` (see the module docstring). ``id`` is taken from
    ``row.job_id`` so the rehydrated Job keeps the same identity its history
    row already carries: the next :func:`~collapsarr.jobs.history.
    record_job_history` call (once this Job actually runs) updates that same
    row in place rather than creating a duplicate.

    Returns a ``(job, None)`` pair on success. Returns ``(None, reason)`` --
    ``reason`` a short, human-readable explanation the caller
    (:func:`rehydrate_pending_jobs`) persists onto the row via
    :func:`_mark_unrehydratable` -- in three cases, each logged as a warning
    here (not raised, so one bad row can't abort rehydrating every other
    one):

    - ``row.job_id`` isn't a valid UUID (a malformed/foreign row -- should
      never happen through this codebase's own write path, but defensive).
    - ``row.kind`` is ``SET_DEFAULT_AUDIO`` and the current
      :class:`~collapsarr.settings.models.GlobalSettings` no longer carries a
      complete Preferred Default Audio preference (it was cleared, or never
      fully configured, since the row was written) -- there is nothing to
      re-derive :attr:`~collapsarr.jobs.queue.Job.preference` from, and
      :meth:`~collapsarr.jobs.queue.JobQueue._run_job` requires a
      ``SET_DEFAULT_AUDIO`` Job to carry one.
    - ``row.kind`` is neither -- unreachable through this codebase's own
      write path (:class:`~collapsarr.jobs.queue.JobKind` has exactly two
      members today), kept as an explicit branch rather than an implicit
      "anything else is DOWNMIX" fallthrough so a future third kind fails
      loudly here instead of silently mislabelled.
    """
    try:
        job_id = UUID(row.job_id)
    except ValueError:
        reason = f"cannot rehydrate: malformed job_id {row.job_id!r}"
        logger.warning("skipping rehydration of job_history id=%s: %s", row.id, reason)
        return None, reason

    if row.kind is JobKind.SET_DEFAULT_AUDIO:
        preference = as_default_audio_preference(global_settings)
        if preference is None:
            reason = (
                "cannot rehydrate: Default Audio Track preference is no "
                "longer fully configured"
            )
            logger.warning(
                "skipping rehydration of job %s (%s): %s", row.job_id, row.file_path, reason
            )
            return None, reason
        return (
            Job(
                file_path=Path(row.file_path),
                settings=DownmixSettings(enabled_targets=frozenset()),
                id=job_id,
                kind=JobKind.SET_DEFAULT_AUDIO,
                preference=preference,
                priority=row.priority,
                status=JobStatus.PENDING,
            ),
            None,
        )

    if row.kind is JobKind.DOWNMIX:
        return (
            Job(
                file_path=Path(row.file_path),
                settings=as_downmix_settings(global_settings),
                id=job_id,
                kind=JobKind.DOWNMIX,
                priority=row.priority,
                status=JobStatus.PENDING,
            ),
            None,
        )

    reason = f"cannot rehydrate: unrecognised job kind {row.kind!r}"  # pragma: no cover
    logger.warning(  # pragma: no cover - JobKind has exactly two members
        "skipping rehydration of job %s (%s): %s", row.job_id, row.file_path, reason
    )
    return None, reason  # pragma: no cover


def _mark_unrehydratable(session: Session, row: JobHistory, reason: str) -> None:
    """Flip a ``PENDING`` row :func:`_reconstruct_job` can't rebuild to ``FAILED`` (COL-166).

    Without this, a row that :func:`_reconstruct_job` can't turn into a Job
    would stay silently ``PENDING`` forever with no backing live Job --
    re-attempted (and re-skipped, for the same reason) on every future
    restart -- exactly the permanent "ghost pending row" this ticket exists
    to eliminate (see the module docstring). ``FAILED`` moves it out of
    ``PENDING`` into a normal, visible terminal state instead, surfaced by
    the Activity/History view like any other failed job, with ``error_text``
    explaining why -- so a user can see it and manually retrigger the work if
    they still want it done, rather than it silently never running again.
    """
    row.status = JobStatus.FAILED
    row.error_text = reason
    row.ended_at = datetime.now(UTC)
    session.commit()
