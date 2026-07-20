"""HTTP REST endpoints for job history & on-demand triggers (COL-29).

Thin layer wrapping the Job Queue & Scheduler epic's service surface, exposed as
a FastAPI :class:`~fastapi.APIRouter` mounted under ``/api`` by
:func:`collapsarr.main.create_app`. Because everything under ``/api`` is gated by
the API-key middleware (COL-26), every route here inherits key-based auth -- no
per-route auth wiring is needed.

Three endpoints, each wrapping an existing service without adding new job logic:

- ``GET /api/jobs/history`` -- lists persisted job history (COL-21,
  :func:`collapsarr.jobs.history.list_job_history`), optionally filtered by
  ``file`` (exact file path) and/or ``status`` (a :class:`~collapsarr.jobs.queue.
  JobStatus` value). Mirrors Sonarr/Radarr's ``/history`` view.
- ``POST /api/jobs/scan`` -- triggers an immediate full-library scan
  (:meth:`collapsarr.jobs.scheduler.JobScheduler.scan_now`, COL-23), enqueuing a
  downmix job for every monitored file that has a qualifying missing target. The
  Sonarr/Radarr analogue is the ``RescanSeries``/``RefreshMovie`` command.
- ``POST /api/jobs/trigger`` -- manually enqueues a downmix job for one specific
  file (:meth:`collapsarr.jobs.scheduler.JobScheduler.trigger_file`, COL-23). The
  optional ``extra_languages`` list is the allow-list-bypass option: those
  languages are forced past the scheduler's ``language_allow_list`` for this one
  call, letting a user downmix a language they normally don't auto-process.

The scan/trigger endpoints operate on the live
:class:`~collapsarr.jobs.scheduler.JobScheduler` the app wired onto
``app.state.job_scheduler`` (see :func:`collapsarr.main.create_app`,
``enable_scheduler=True``) so a manual trigger and the background loop share one
queue. When no scheduler is wired (e.g. an app built without the scheduler), the
dependency raises ``503`` rather than silently doing nothing.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_session
from .history import list_job_history
from .models import JobHistory
from .queue import Job, JobStatus
from .scheduler import JobScheduler

router = APIRouter(prefix="/api", tags=["jobs"])


# --- dependencies ------------------------------------------------------------


def get_job_scheduler(request: Request) -> JobScheduler:
    """Return the app's live :class:`JobScheduler`, or ``503`` if none is wired.

    The scan/trigger endpoints act on the same scheduler (and its shared queue)
    the background loop drains, exposed on ``app.state.job_scheduler`` by
    :func:`collapsarr.main.create_app` when built with ``enable_scheduler=True``
    (the production path). An app built without it has no on-demand trigger
    surface, so we fail loudly with ``503`` instead of pretending to enqueue.
    """
    scheduler: JobScheduler | None = getattr(request.app.state, "job_scheduler", None)
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="Job scheduler is not available.",
        )
    return scheduler


# --- schemas -----------------------------------------------------------------


class JobHistoryRead(BaseModel):
    """Response shape for one persisted job-history row (COL-21)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: str
    file_path: str
    status: JobStatus
    started_at: datetime | None
    ended_at: datetime | None
    exit_code: int | None
    error_text: str | None
    target: str | None
    language: str | None
    created_at: datetime
    updated_at: datetime


class EnqueuedJob(BaseModel):
    """The identifying summary of a job the scheduler just enqueued in-memory."""

    id: str
    file_path: str
    status: JobStatus

    @classmethod
    def from_job(cls, job: Job) -> EnqueuedJob:
        return cls(id=str(job.id), file_path=str(job.file_path), status=job.status)


class ScanResult(BaseModel):
    """Response for ``POST /api/jobs/scan``: the jobs the scan pass enqueued."""

    enqueued: list[EnqueuedJob]


class ManualTriggerRequest(BaseModel):
    """Request body for ``POST /api/jobs/trigger``.

    ``file_path`` names the (host-local) file to downmix. ``extra_languages`` is
    the allow-list-bypass option (COL-23): languages listed here are unioned onto
    the scheduler's ``language_allow_list`` for this one call, so a language the
    global allow-list would otherwise exclude still gets a downmix job. When the
    scheduler has no allow-list (every language is already eligible) it has no
    effect. Omit it (or send ``[]``) for a plain trigger honouring the allow-list.
    """

    model_config = ConfigDict(extra="forbid")

    file_path: str
    extra_languages: list[str] = []


class ManualTriggerResult(BaseModel):
    """Response for ``POST /api/jobs/trigger``.

    ``enqueued`` is ``True`` with the created ``job`` when a downmix job was
    queued. It is ``False`` with ``job`` ``null`` when the file was skipped -- a
    duplicate (already queued / recently processed), unprobeable, or with no
    qualifying target even after ``extra_languages`` -- mirroring
    :meth:`collapsarr.jobs.scheduler.JobScheduler.trigger_file` returning ``None``.
    """

    enqueued: bool
    job: EnqueuedJob | None


# --- endpoints ---------------------------------------------------------------


@router.get("/jobs/history", response_model=list[JobHistoryRead])
def list_job_history_endpoint(
    file: str | None = None,
    status: JobStatus | None = None,
    session: Session = Depends(get_session),
) -> list[JobHistory]:
    """List persisted job history, optionally filtered by file and/or status.

    ``file`` matches a file path exactly (the form job history stores);
    ``status`` matches a single :class:`~collapsarr.jobs.queue.JobStatus`
    (``pending``/``running``/``succeeded``/``failed``). Both may be combined;
    omitting both returns every row, ordered by insertion.
    """
    return list_job_history(session, file_path=file, status=status)


@router.post("/jobs/scan", response_model=ScanResult, status_code=202)
def scan_now_endpoint(scheduler: JobScheduler = Depends(get_job_scheduler)) -> ScanResult:
    """Trigger an immediate full-library scan, returning the jobs it enqueued.

    Runs the same scan the periodic loop runs (COL-22/COL-23), synchronously:
    every configured instance's monitored files are re-probed and a downmix job
    is enqueued for each with a qualifying missing target (skipped/no-op files
    excluded). ``202 Accepted`` -- the jobs are queued, not yet run.
    """
    jobs = scheduler.scan_now()
    return ScanResult(enqueued=[EnqueuedJob.from_job(job) for job in jobs])


@router.post("/jobs/trigger", response_model=ManualTriggerResult, status_code=202)
def manual_trigger_endpoint(
    body: ManualTriggerRequest,
    scheduler: JobScheduler = Depends(get_job_scheduler),
) -> ManualTriggerResult:
    """Manually enqueue a downmix job for one file, honouring the bypass option.

    Wraps :meth:`collapsarr.jobs.scheduler.JobScheduler.trigger_file`: probes the
    file, enqueues a job when a target qualifies, and threads ``extra_languages``
    through as the allow-list-bypass. A ``202`` is returned whether or not a job
    was enqueued; the ``enqueued`` flag distinguishes the two (a skipped file --
    duplicate/unprobeable/nothing to do -- is not an error).
    """
    job = scheduler.trigger_file(
        body.file_path,
        extra_languages=body.extra_languages or None,
    )
    if job is None:
        return ManualTriggerResult(enqueued=False, job=None)
    return ManualTriggerResult(enqueued=True, job=EnqueuedJob.from_job(job))
