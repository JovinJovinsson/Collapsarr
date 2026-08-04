"""Job queue and scheduler: concurrency control, job history (COL-3+).

Home for job-queue-and-scheduler concerns per ``docs/TRACKER.md``: enqueueing
and running the Downmix Engine's pipeline per file under a configurable
concurrency cap (COL-20), periodic full-library scans, and job history
(COL-21).

This module is imported for its side effect of registering
:class:`~collapsarr.jobs.models.JobHistory` with
:data:`collapsarr.database.Base.metadata` -- see
the Alembic migration environment (:mod:`collapsarr.migrations`).
"""

from __future__ import annotations

from .failure_notify import make_failure_notifier, notify_job_failure
from .history import (
    get_job_history,
    list_job_history,
    make_history_recorder,
    record_job_history,
)
from .models import JobHistory
from .queue import (
    DEFAULT_MAX_CONCURRENCY,
    FailureNotifier,
    HistoryRecorder,
    Job,
    JobQueue,
    JobStatus,
    PipelineRunner,
    TrackedMediaRecorder,
)
from .tracked_media import make_tracked_media_recorder, record_tracked_media

__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "FailureNotifier",
    "HistoryRecorder",
    "Job",
    "JobHistory",
    "JobQueue",
    "JobStatus",
    "PipelineRunner",
    "TrackedMediaRecorder",
    "get_job_history",
    "list_job_history",
    "make_failure_notifier",
    "make_history_recorder",
    "make_tracked_media_recorder",
    "notify_job_failure",
    "record_job_history",
    "record_tracked_media",
]
