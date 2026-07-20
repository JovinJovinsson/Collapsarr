"""Job queue and scheduler: concurrency control, job history (COL-3+).

Home for job-queue-and-scheduler concerns per ``docs/TRACKER.md``: enqueueing
and running the Downmix Engine's pipeline per file under a configurable
concurrency cap (COL-20), periodic full-library scans, and job history.
"""

from __future__ import annotations

from .queue import DEFAULT_MAX_CONCURRENCY, Job, JobQueue, JobStatus, PipelineRunner

__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "Job",
    "JobQueue",
    "JobStatus",
    "PipelineRunner",
]
