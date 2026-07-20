"""Job queue and scheduler: concurrency control, job history (COL-3+).

Home for job-queue-and-scheduler concerns per ``docs/TRACKER.md``: enqueueing
and running the Downmix Engine's pipeline per file under a configurable
concurrency cap (COL-20), periodic full-library scans, and job history
(COL-21).

This module is imported for its side effect of registering
:class:`~collapsarr.jobs.models.JobHistory` with
:data:`collapsarr.database.Base.metadata` -- see
:func:`collapsarr.database.init_db`.
"""

from __future__ import annotations

from .history import get_job_history, list_job_history, record_job_history
from .models import JobHistory
from .queue import DEFAULT_MAX_CONCURRENCY, Job, JobQueue, JobStatus, PipelineRunner

__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "Job",
    "JobHistory",
    "JobQueue",
    "JobStatus",
    "PipelineRunner",
    "get_job_history",
    "list_job_history",
    "record_job_history",
]
