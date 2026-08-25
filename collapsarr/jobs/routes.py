"""HTTP REST endpoints for job history & on-demand triggers (COL-29).

Thin layer wrapping the Job Queue & Scheduler epic's service surface, exposed as
a FastAPI :class:`~fastapi.APIRouter` mounted under ``/api`` by
:func:`collapsarr.main.create_app`. Because everything under ``/api`` is gated by
the API-key middleware (COL-26), every route here inherits key-based auth -- no
per-route auth wiring is needed.

Twelve endpoints, each wrapping an existing service without adding new job logic:

- ``GET /api/jobs/history`` -- lists persisted job history (COL-21,
  :func:`collapsarr.jobs.history.list_job_history`), optionally filtered by
  ``file`` (exact file path) and/or ``status`` (a :class:`~collapsarr.jobs.queue.
  JobStatus` value). Mirrors Sonarr/Radarr's ``/history`` view.
- ``GET /api/jobs/queue`` -- lists every currently ``running``/``pending`` Job
  together (COL-175, :func:`collapsarr.jobs.history.list_queue_jobs`),
  ordered running-first then pending by ascending ``priority`` -- the shape
  the Queue page (COL-176) needs and ``GET /api/jobs/history`` deliberately
  doesn't provide (that endpoint's ``status`` filter is single-valued and its
  ordering is insertion order, unchanged for the existing History page's
  ``fetchJobHistory`` contract). Same response shape as ``GET
  /api/jobs/history`` (:class:`JobHistoryRead`, now including ``priority``).
- ``POST /api/jobs/scan`` -- triggers an immediate full-library scan
  (:meth:`collapsarr.jobs.scheduler.JobScheduler.scan_now`, COL-23), enqueuing a
  downmix job for enough monitored files with a qualifying missing target to
  reach the Auto-Queue Limit (COL-171, fixed at 5 total ``PENDING`` jobs) --
  not every qualifying file, as it did before COL-171. The Sonarr/Radarr
  analogue is the ``RescanSeries``/``RefreshMovie`` command.
- ``POST /api/jobs/trigger`` -- manually enqueues a downmix job for one specific
  file (:meth:`collapsarr.jobs.scheduler.JobScheduler.trigger_file`, COL-23). The
  optional ``extra_languages`` list is the allow-list-bypass option: those
  languages are forced past the scheduler's ``language_allow_list`` for this one
  call, letting a user downmix a language they normally don't auto-process. As of
  COL-170 it also always bypasses the Recently-Processed Window (COL-167) --
  a behavior change from before, when it respected the window like every other
  trigger: every single, explicit trigger is now treated as a deliberate
  request that overrides the cooldown, matching ``POST /api/jobs/requeue``
  below. It still goes through the same qualifying-target detection either
  way -- a file with nothing to do is still skipped.
- ``POST /api/jobs/requeue`` -- the per-row "Requeue" action (COL-170):
  manually enqueues a downmix job for one specific file -- typically a
  previously-failed one a user is retrying from the Activity/History view --
  via :meth:`collapsarr.jobs.scheduler.JobScheduler.requeue_file`, which
  always bypasses the Recently-Processed Window. Same response shape as
  ``POST /api/jobs/trigger``; the only difference is the window is *always*
  bypassed here (there is no honour-the-window option), and there is no
  ``extra_languages`` override -- a requeue retries against the standing
  language allow-list, not a one-off widened one. The window bypass is the
  only thing it changes: the same qualifying-target detection still applies,
  so a file with nothing to do (already fully downmixed) is still skipped,
  and a file with a job already ``PENDING``/``RUNNING`` right now is still
  reported as not enqueued.
- ``POST /api/jobs/requeue-failed`` -- the batch "Requeue all failed" action
  (COL-172): requeues every currently-``FAILED`` ``DOWNMIX`` Job in one call
  via :meth:`collapsarr.jobs.scheduler.JobScheduler.requeue_all_failed` (a
  ``SET_DEFAULT_AUDIO`` failure is out of scope -- ``POST
  /api/jobs/trigger-default-audio/bulk`` above is its own bulk retry entry
  point). Unlike ``POST /api/jobs/requeue`` above, this *respects* the
  Recently-Processed Window (COL-167) -- a bulk retry of every failed file is
  closer in spirit to the automatic paths that window protects against than
  to one explicit single-file action, so a file whose most recent terminal
  history row falls inside the window is skipped, not requeued. The response
  reports the full split -- every currently-failed file's path lands in
  either ``requeued`` (with its newly created job) or ``skipped`` -- so an
  all-skipped pass (e.g. every failed file failed too recently) is a valid,
  fully-reported outcome, not an error.
- ``POST /api/jobs/trigger-default-audio`` -- manually enqueues a
  ``SET_DEFAULT_AUDIO`` job for one specific file
  (:meth:`collapsarr.jobs.scheduler.JobScheduler.trigger_set_default_audio`,
  COL-155), mirroring ``POST /api/jobs/trigger``'s shape: same request
  (a bare ``file_path``), same response shape (``enqueued`` + the job, or
  ``enqueued=False``/``job=null`` when the file needs no change). As of
  COL-206 it also always bypasses the Recently-Processed Window (COL-167) --
  a behavior change from before, when it respected the window like every
  other trigger -- matching ``POST /api/jobs/trigger``'s own COL-170
  behavior: an explicit single-file trigger is now always a deliberate
  request that overrides the cooldown, not silently no-op'd by an unrelated
  prior job (e.g. a ``DOWNMIX`` job) on the same file.
- ``POST /api/jobs/trigger-default-audio/bulk`` -- the multi-select
  counterpart of the above (COL-156): accepts one or more Library
  ``{node_type, node_id}`` references (the same shape
  ``POST /api/library/tracked``'s bulk Tracked-update endpoint takes,
  :mod:`collapsarr.library.routes`, COL-101), cascades any Series/Season
  reference down to its descendant Episode/Movie leaves exactly the way that
  endpoint's underlying :func:`~collapsarr.library.service.set_tracked`
  cascades, resolves each leaf to its known file path (COL-154's
  Library-to-tracked-media bridge), de-duplicates (a file reachable via more
  than one selected reference is only triggered once), and calls
  :meth:`~collapsarr.jobs.scheduler.JobScheduler.trigger_set_default_audio`
  once per resulting file -- always against the current global Preferred
  Default Audio setting; there is no per-call override, unlike
  ``trigger``'s ``extra_languages``. Unlike the single-file endpoint above,
  this one does **not** pass ``bypass_dedup_window`` (COL-206) -- it still
  respects the Recently-Processed Window, the same rationale as ``POST
  /api/jobs/requeue-failed``'s bulk behavior: a bulk trigger across a
  multi-select is closer in spirit to the automatic paths the window
  protects against than to one explicit single-file action.
- ``DELETE /api/jobs/{job_id}`` -- cancels one specific Job, ``PENDING`` or
  ``RUNNING`` (COL-168; hard-kill COL-192)
  (:meth:`collapsarr.jobs.scheduler.JobScheduler.cancel_job`). Cancelling a
  still-``PENDING`` Job is deletion, not a new status: the Job's persisted
  :class:`~collapsarr.jobs.models.JobHistory` row is deleted too -- a
  never-started Job leaves no trace, no audit row, no cooldown interaction
  with the Recently-Processed Window. Cancelling a Job a worker is already
  *running* (COL-192) hard-kills its in-flight ffmpeg/ffprobe subprocess (and
  children) immediately, freeing the worker slot for the next queued Job; the
  killed run transitions to ``FAILED`` and keeps its history row (a
  hard-cancel is recorded as a failure -- there is no distinct ``CANCELLED``
  status). Both report ``cancelled=True``. Only a Job that finished naturally
  in the race between the request landing and the cancel running is reported
  ``cancelled=False`` ("too late"); see :class:`CancelJobResult`. A ``job_id``
  not present in the live queue at all -- unknown, malformed, or already gone
  -- is a ``404``.
- ``POST /api/jobs/clear`` -- the batch "Clear queue" cancel action (COL-173):
  cancels every currently-``PENDING`` Job in one call via
  :meth:`collapsarr.jobs.scheduler.JobScheduler.clear_queue` (the bulk
  counterpart of ``DELETE /api/jobs/{job_id}`` above, mirroring ``POST
  /api/jobs/requeue-failed``'s "one dedicated scheduler method, thin route"
  shape). Unlike that per-row cancel, the response reports *counts* rather
  than a single boolean -- ``cancelled`` (how many were actually removed
  from the live queue) vs. ``already_running`` (how many had already been
  claimed by a worker, or otherwise progressed past ``PENDING``, by the time
  their own cancel ran, and so were left alone) -- since the queue's worker
  pool keeps running concurrently with no push mechanism to freeze it
  mid-request, only polling. Because cancelling frees a slot exactly like
  any other cancellation or completion, the Auto-Queue Limit's top-up
  (COL-171) runs once, immediately after the whole batch -- clearing the
  queue is not a pause, it simply resets to whatever the scanner refills
  next (Auto-Queuing Pause, COL-174, is the dedicated lever for that, not
  implemented here). Clearing an already-empty queue is not an error -- a
  valid ``cancelled=0``/``already_running=0`` response.
- ``POST /api/jobs/{job_id}/bump`` -- bumps one still-``PENDING`` Job to the
  front of the queue (COL-169)
  (:meth:`collapsarr.jobs.scheduler.JobScheduler.bump_job_to_front`), the only
  reordering primitive in scope -- there is no general "move to an arbitrary
  position". Wraps :meth:`~collapsarr.jobs.queue.JobQueue.bump_to_front`,
  which reassigns the Job's priority below every other pending Job's, so it
  is the very next Job a free worker claims. Mirrors the ``DELETE``
  endpoint's result shape: a Job that's no longer ``PENDING`` (already
  claimed/terminal) reports ``bumped=False`` rather than erroring (see
  :class:`BumpJobResult`), and a ``job_id`` not present in the live queue at
  all is a ``404``, same as the ``DELETE`` endpoint.
- ``POST /api/jobs/process-now`` -- the per-row "Process Now" action
  (COL-229): force-starts a downmix Job for one file *immediately*
  (:meth:`collapsarr.jobs.scheduler.JobScheduler.process_now`), bypassing
  both Auto-Processing Pause and the Concurrency Limit -- unlike ``POST
  /api/jobs/{job_id}/bump`` above, which only reorders within the existing
  pending queue and stays subject to both once a pool worker actually claims
  it. Creates a pending Job first when the file has none yet, the same way
  ``POST /api/jobs/trigger`` does. When force-starting would push the number
  of currently-``RUNNING`` Jobs past the configured Concurrency Limit,
  responds with ``needs_confirmation=True`` and starts nothing
  (:meth:`collapsarr.jobs.scheduler.JobScheduler.would_exceed_concurrency_limit`,
  checked first); the caller re-submits with ``confirm=True`` to proceed
  anyway.

The scan/trigger endpoints operate on the live
:class:`~collapsarr.jobs.scheduler.JobScheduler` the app wired onto
``app.state.job_scheduler`` (see :func:`collapsarr.main.create_app`,
``enable_scheduler=True``) so a manual trigger and the background loop share one
queue. When no scheduler is wired (e.g. an app built without the scheduler), the
dependency raises ``503`` rather than silently doing nothing.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..database import get_session
from ..library.models import LibraryNode, LibraryNodeKind
from ..library.service import get_node, list_nodes
from ..media.service import list_tracked_media_by_instance
from .history import list_job_history, list_queue_jobs
from .models import JobHistory
from .queue import Job, JobKind, JobStatus
from .scheduler import DefaultAudioSkipReason, JobScheduler

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
    """Response shape for one persisted job-history row (COL-21).

    ``priority`` (COL-163, exposed here as of COL-175) mirrors the
    originating :class:`~collapsarr.jobs.queue.Job`'s
    :attr:`~collapsarr.jobs.queue.Job.priority` -- the join-order sequence
    number a lower value means "earlier"/"next in line". It's what
    ``GET /api/jobs/queue`` (COL-175) orders pending Jobs by.

    ``scheduled_at`` (COL-242) is ``null`` for every existing Job kind/
    trigger -- nothing yet enqueues one with a due-time gate. When set on a
    row whose ``status`` is still ``pending``, the frontend derives a
    ``SCHEDULED`` display label (and shows the due date/time) in place of
    ``PENDING`` if ``scheduled_at`` is still in the future; the backend
    ``status`` itself never changes to a new value for this -- see
    :class:`~collapsarr.jobs.models.JobHistory`'s own docstring.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: str
    file_path: str
    status: JobStatus
    kind: JobKind
    priority: int
    scheduled_at: datetime | None
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


class RequeueFileRequest(BaseModel):
    """Request body for ``POST /api/jobs/requeue`` (COL-170).

    ``file_path`` names the (host-local) file to requeue -- typically one
    with a ``FAILED`` job-history row a user is retrying from the
    Activity/History view, though nothing here validates that; it goes
    through the same probe/qualifying-target sequence as any other trigger.
    Unlike :class:`ManualTriggerRequest`, there is no ``extra_languages``
    option -- a requeue retries against the standing language allow-list,
    not a one-off widened one.
    """

    model_config = ConfigDict(extra="forbid")

    file_path: str


class RequeueFileResult(BaseModel):
    """Response for ``POST /api/jobs/requeue`` (COL-170).

    ``enqueued`` is ``True`` with the created ``job`` when a downmix job was
    queued. It is ``False`` with ``job`` ``null`` when the file was skipped --
    a duplicate (already queued/running -- the Recently-Processed Window is
    always bypassed here, so it can never be the reason), unprobeable, or
    with no qualifying target -- mirroring
    :meth:`collapsarr.jobs.scheduler.JobScheduler.requeue_file` returning
    ``None``.
    """

    enqueued: bool
    job: EnqueuedJob | None


class BulkRequeueFailedResult(BaseModel):
    """Response for ``POST /api/jobs/requeue-failed`` (COL-172).

    ``requeued`` lists every newly created job for a currently-``FAILED``
    file this pass did *not* skip. ``skipped`` lists every currently-``FAILED``
    file's path this pass did *not* requeue -- most commonly because its most
    recent terminal history row falls inside the Recently-Processed Window
    (COL-167), but also any other reason
    :meth:`collapsarr.jobs.scheduler.JobScheduler.trigger_file` might decline
    a file (already active, unprobeable, or nothing left to do). Every
    currently-failed file lands in exactly one of the two lists -- never a
    silent partial success -- so an all-skipped response (e.g. every failed
    file failed too recently) is a valid, fully-reported outcome rather than
    an error.
    """

    requeued: list[EnqueuedJob]
    skipped: list[str]


class SetDefaultAudioTriggerRequest(BaseModel):
    """Request body for ``POST /api/jobs/trigger-default-audio`` (COL-155).

    ``file_path`` names the (host-local) file to fix. Unlike
    :class:`ManualTriggerRequest` there is no allow-list-bypass option --
    the Default Audio Track fix isn't gated by a language allow-list at all,
    so there is nothing analogous to bypass.
    """

    model_config = ConfigDict(extra="forbid")

    file_path: str


class SetDefaultAudioTriggerResult(BaseModel):
    """Response for ``POST /api/jobs/trigger-default-audio`` (COL-155; ``skip_reason`` COL-207).

    ``enqueued`` is ``True`` with the created ``job`` when a
    ``SET_DEFAULT_AUDIO`` job was queued, and ``skip_reason`` is ``null``. It
    is ``False`` with ``job`` ``null`` when the file was skipped, and
    ``skip_reason`` names which :class:`~collapsarr.jobs.scheduler.
    DefaultAudioSkipReason` explains why -- no Default Audio Track preference
    is configured, a duplicate (already queued / recently processed --
    reachable here only via the still-unbypassable active-job check, since
    this endpoint always bypasses the Recently-Processed Window, COL-206),
    unprobeable, or the file already carries the correct disposition --
    mirroring :meth:`collapsarr.jobs.scheduler.JobScheduler.
    trigger_set_default_audio` returning a :class:`~collapsarr.jobs.
    scheduler.SetDefaultAudioOutcome`.
    """

    enqueued: bool
    job: EnqueuedJob | None
    skip_reason: DefaultAudioSkipReason | None = None


class DefaultAudioNodeReference(BaseModel):
    """One ``{node_type, node_id}`` reference in a bulk trigger request (COL-156).

    Identical shape to :class:`~collapsarr.library.routes.TrackedNodeReference`
    (COL-101) -- a client-side ``node_type``/actual-kind mismatch is a
    ``422``, not silently corrected, for the same reason: it usually means
    the caller is pointing at the wrong node entirely.
    """

    node_type: LibraryNodeKind
    node_id: int


class BulkSetDefaultAudioTriggerRequest(BaseModel):
    """Body for ``POST /api/jobs/trigger-default-audio/bulk`` (COL-156).

    One or more Library node references. Unlike
    :class:`SetDefaultAudioTriggerRequest`, there is no ``file_path`` option
    and no override of any kind -- every resolved file is triggered against
    whatever the current global Preferred Default Audio setting is at
    request time.
    """

    model_config = ConfigDict(extra="forbid")

    references: list[DefaultAudioNodeReference] = Field(min_length=1)


class FileSetDefaultAudioResult(SetDefaultAudioTriggerResult):
    """One resolved file's outcome within a bulk trigger response (COL-156; COL-207 skip_reason).

    Extends :class:`SetDefaultAudioTriggerResult` with the ``file_path``
    identifying which resolved file this result belongs to (the bulk
    response has no other way to attribute an outcome back to a specific
    file) -- so the two response shapes share one definition of the
    ``enqueued``/``job``/``skip_reason`` triple rather than two copies that
    could drift. Unlike the single-file endpoint, this one never bypasses
    the Recently-Processed Window, so ``skip_reason=DUPLICATE`` here can
    mean either the active-job check or the window -- see
    :class:`~collapsarr.jobs.scheduler.DefaultAudioSkipReason`.
    """

    file_path: str


class BulkSetDefaultAudioTriggerResult(BaseModel):
    """Response for ``POST /api/jobs/trigger-default-audio/bulk`` (COL-156).

    One :class:`FileSetDefaultAudioResult` per unique file the request's
    references resolved to -- see
    :func:`_resolve_default_audio_leaf_files` for how references cascade and
    de-duplicate down to that file set.
    """

    results: list[FileSetDefaultAudioResult]


class CancelJobResult(BaseModel):
    """Response for ``DELETE /api/jobs/{job_id}`` (COL-168; hard-kill COL-192).

    ``cancelled`` is ``True`` in both success shapes: a still-``PENDING`` Job
    removed from the live queue with its
    :class:`~collapsarr.jobs.models.JobHistory` row deleted (leaving no trace
    at all), **or** a ``RUNNING`` Job whose in-flight subprocess was hard-killed
    (COL-192), freeing its worker slot -- that run transitions to ``FAILED``
    and keeps its history row (a hard-cancel is recorded as a failure, not a
    distinct ``CANCELLED`` status). It is ``False`` -- not an error -- only
    when the Job finished naturally in the race between the request landing and
    the cancel running: "too late" to cancel, distinct from both success and a
    generic failure; the finished Job's history row is left intact. A ``job_id``
    not present in the live queue at all -- unknown, malformed, or already gone
    -- is reported as ``404``, not this shape (there is nothing to act on
    either way).
    """

    cancelled: bool


class ClearQueueResult(BaseModel):
    """Response for ``POST /api/jobs/clear`` (COL-173).

    ``cancelled`` is how many currently-``PENDING`` Jobs, out of every one
    snapshotted at the start of the pass, this call actually cancelled.
    ``already_running`` is how many of that same snapshot had already been
    claimed by a worker (or otherwise progressed past ``PENDING``) by the
    time their individual cancel ran, and so were left alone -- reported
    rather than silently ignored, mirroring
    :meth:`collapsarr.jobs.scheduler.JobScheduler.clear_queue`'s
    same-named fields (see there for the full ``ClearQueueResult`` contract).
    Every snapshotted Job lands in exactly one of the two counts. Clearing
    an already-empty queue is not an error -- a valid
    ``cancelled=0``/``already_running=0`` response.
    """

    cancelled: int
    already_running: int


class BumpJobResult(BaseModel):
    """Response for ``POST /api/jobs/{job_id}/bump`` (COL-169).

    ``bumped`` is ``True`` when the Job was still ``PENDING`` and has now
    been reassigned a priority ahead of every other currently-pending Job --
    it is the very next Job a free worker claims. It is ``False`` -- not an
    error -- when the Job still exists but is no longer ``PENDING`` (a
    worker already claimed it, or it has already reached a terminal status):
    "too late" to bump, distinct from both success and a generic failure;
    the already-claimed/finished Job runs (or has run) to completion
    normally, unaffected. A ``job_id`` not present in the live queue at all
    -- unknown, malformed, or already gone -- is reported as ``404``, not
    this shape (there is nothing to act on either way). Mirrors
    :class:`CancelJobResult`'s shape exactly.
    """

    bumped: bool


class ProcessNowRequest(BaseModel):
    """Request body for ``POST /api/jobs/process-now`` (COL-229).

    ``file_path`` names the (host-local) file to force-start -- the same
    file a queue row's ``file_path`` names, or a Wanted file with no
    existing Job at all. ``confirm`` defaults to ``False``: the first
    request for a file that would push the number of currently-``RUNNING``
    Jobs past the configured Concurrency Limit is answered with
    :attr:`ProcessNowResult.needs_confirmation` set and nothing started (see
    that field's own doc comment); the frontend re-submits the same request
    with ``confirm=True`` to force-start it over the limit anyway.
    """

    model_config = ConfigDict(extra="forbid")

    file_path: str
    confirm: bool = False


class ProcessNowResult(BaseModel):
    """Response for ``POST /api/jobs/process-now`` (COL-229).

    ``needs_confirmation=True`` (always paired with ``enqueued=False``,
    ``job=null``) means starting this Job right now would push the number of
    currently-``RUNNING`` Jobs past the configured Concurrency Limit --
    checked via :meth:`~collapsarr.jobs.scheduler.JobScheduler.
    would_exceed_concurrency_limit` *before* anything else, so nothing was
    created or started; declining (simply not re-submitting) leaves the file
    exactly as it was. Resubmit the same request with ``confirm=True`` to
    force-start it anyway.

    Otherwise ``needs_confirmation`` is ``False`` and ``enqueued`` carries
    the usual meaning: ``True`` with the acted-on ``job`` (its ``status``
    already ``running``) when a Job for the file was found and force-started,
    or newly created and force-started. ``False`` with ``job`` ``null`` when
    the file was skipped -- unprobeable, or no qualifying target -- mirroring
    :meth:`~collapsarr.jobs.scheduler.JobScheduler.process_now` returning
    ``None``.
    """

    enqueued: bool
    job: EnqueuedJob | None
    needs_confirmation: bool = False


# --- endpoints ---------------------------------------------------------------


def _resolve_default_audio_leaf_files(
    session: Session, references: list[DefaultAudioNodeReference]
) -> list[str]:
    """Resolve a bulk request's node references down to their unique file paths (COL-156).

    Mirrors ``POST /api/library/tracked``'s bulk Tracked-update endpoint
    (:mod:`collapsarr.library.routes`, COL-101): every reference is resolved
    and validated *before* anything else happens -- ``404`` if any
    ``node_id`` doesn't exist, ``422`` if any reference's ``node_type``
    doesn't match that node's actual kind.

    A Series/Season reference then cascades to its descendant Episode/Movie
    nodes, the same ``parent_id``-stack walk over
    :func:`~collapsarr.library.service.list_nodes`'s full (hidden-included)
    node set that :func:`~collapsarr.library.service.set_tracked` uses to
    cascade a Tracked write to descendants -- a hidden descendant
    (soft-hidden by a later sync, not deleted) still has a real on-disk file
    and a ``tracked_override`` that can resolve, so it stays in scope here
    too; an Episode/Movie reference is already a leaf. Leaves are
    deduplicated by node id first (so a file reachable via two references --
    e.g. a Series reference and a standalone Episode reference inside it --
    is only counted once), then each leaf is bridged
    to its known on-disk file path via the same Library-to-tracked-media
    bridge :func:`~collapsarr.library.service.build_tree`/
    :func:`~collapsarr.library.service.build_movie_tree` use for the
    current-default-track column (COL-154): one bulk
    :func:`~collapsarr.media.service.list_tracked_media_by_instance` fetch
    per distinct instance among the resolved leaves, keyed by
    ``sonarr_episode_id``/``radarr_movie_id``. A leaf with no bridged row --
    never scanned/imported, so no on-disk file is known yet -- contributes
    nothing to the result; there is no file path to trigger against.

    Returns the unique file paths, in first-seen order.
    """
    roots: list[LibraryNode] = []
    for reference in references:
        node = get_node(session, reference.node_id)
        if node is None:
            raise HTTPException(
                status_code=404, detail=f"No library node with id={reference.node_id}"
            )
        if node.kind is not reference.node_type:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"node_id={reference.node_id} is a {node.kind.value!r} node, "
                    f"not {reference.node_type.value!r}"
                ),
            )
        roots.append(node)

    leaves_by_id: dict[int, LibraryNode] = {}
    nodes_by_instance: dict[int, list[LibraryNode]] = {}
    for root in roots:
        if root.kind in (LibraryNodeKind.EPISODE, LibraryNodeKind.MOVIE):
            leaves_by_id[root.id] = root
            continue

        # Series/Season: cascade to descendant Episode leaves.
        if root.instance_id not in nodes_by_instance:
            nodes_by_instance[root.instance_id] = list_nodes(session, root.instance_id)
        children_by_parent: dict[int, list[LibraryNode]] = defaultdict(list)
        for candidate in nodes_by_instance[root.instance_id]:
            if candidate.parent_id is not None:
                children_by_parent[candidate.parent_id].append(candidate)

        stack = list(children_by_parent[root.id])
        while stack:
            descendant = stack.pop()
            if descendant.kind in (LibraryNodeKind.EPISODE, LibraryNodeKind.MOVIE):
                leaves_by_id[descendant.id] = descendant
            else:
                stack.extend(children_by_parent[descendant.id])

    media_by_instance: dict[int, dict[int, str]] = {}
    file_paths: dict[str, None] = {}  # insertion-ordered de-dup set
    for leaf in leaves_by_id.values():
        if leaf.instance_id not in media_by_instance:
            by_source_id: dict[int, str] = {}
            for media in list_tracked_media_by_instance(session, leaf.instance_id):
                if media.sonarr_episode_id is not None:
                    by_source_id[media.sonarr_episode_id] = media.file_path
                elif media.radarr_movie_id is not None:
                    by_source_id[media.radarr_movie_id] = media.file_path
            media_by_instance[leaf.instance_id] = by_source_id

        if leaf.sonarr_episode_id is not None:
            source_id: int | None = leaf.sonarr_episode_id
        elif leaf.radarr_movie_id is not None:
            source_id = leaf.radarr_movie_id
        else:
            source_id = None
        if source_id is None:
            continue

        file_path = media_by_instance[leaf.instance_id].get(source_id)
        if file_path is not None:
            file_paths.setdefault(file_path, None)

    return list(file_paths)


@router.get("/jobs/history", response_model=list[JobHistoryRead])
def list_job_history_endpoint(
    file: str | None = None,
    status: JobStatus | None = None,
    kind: JobKind | None = None,
    session: Session = Depends(get_session),
) -> list[JobHistory]:
    """List persisted job history, optionally filtered by file, status, and/or kind.

    ``file`` matches a file path exactly (the form job history stores);
    ``status`` matches a single :class:`~collapsarr.jobs.queue.JobStatus`
    (``pending``/``running``/``succeeded``/``failed``); ``kind`` (COL-155)
    matches a single :class:`~collapsarr.jobs.queue.JobKind`
    (``downmix``/``set_default_audio``). Any combination may be given;
    omitting all returns every row, ordered by insertion.
    """
    return list_job_history(session, file_path=file, status=status, kind=kind)


@router.get("/jobs/queue", response_model=list[JobHistoryRead])
def list_job_queue_endpoint(session: Session = Depends(get_session)) -> list[JobHistory]:
    """List every currently ``running``/``pending`` Job together, queue-ordered (COL-175).

    Wraps :func:`collapsarr.jobs.history.list_queue_jobs`: only
    ``RUNNING``/``PENDING`` rows are returned (a terminal row has left the
    queue), ordered **running first**, then **pending ordered by ascending
    priority** -- the shape the Queue page (COL-176) needs. This is a
    separate, additive endpoint rather than a change to ``GET
    /api/jobs/history``'s existing single-status filter/insertion-order
    contract, so ``fetchJobHistory`` (the History page's fetch-all-and-filter
    client-side approach) is unaffected.
    """
    return list_queue_jobs(session)


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

    Always passes ``bypass_dedup_window=True`` (COL-170) -- a behavior change
    from before COL-170, when this endpoint respected the Recently-Processed
    Window like every other trigger: every single, explicit trigger is now
    treated as a deliberate request that overrides the cooldown (matching
    ``POST /api/jobs/requeue`` below). The window is the only thing bypassed;
    the qualifying-target gate is unchanged, so a file with nothing to do is
    still skipped.
    """
    job = scheduler.trigger_file(
        body.file_path,
        extra_languages=body.extra_languages or None,
        bypass_dedup_window=True,
    )
    if job is None:
        return ManualTriggerResult(enqueued=False, job=None)
    return ManualTriggerResult(enqueued=True, job=EnqueuedJob.from_job(job))


@router.post("/jobs/requeue", response_model=RequeueFileResult, status_code=202)
def requeue_file_endpoint(
    body: RequeueFileRequest,
    scheduler: JobScheduler = Depends(get_job_scheduler),
) -> RequeueFileResult:
    """Requeue one specific file -- the per-row "Requeue" action (COL-170).

    Wraps :meth:`collapsarr.jobs.scheduler.JobScheduler.requeue_file`, which
    always bypasses the Recently-Processed Window (COL-167) regardless of its
    current value -- the intended use is a user clicking "Requeue" on a
    specific failed file from the Activity/History view, an explicit request
    that should never be silently swallowed by the cooldown. It still goes
    through the same qualifying-target detection as every other trigger, so a
    file with nothing to do (already fully downmixed) is still skipped, and a
    file with a job already ``PENDING``/``RUNNING`` right now is still a
    duplicate. A ``202`` is returned whether or not a job was enqueued; the
    ``enqueued`` flag distinguishes the two.
    """
    job = scheduler.requeue_file(body.file_path)
    if job is None:
        return RequeueFileResult(enqueued=False, job=None)
    return RequeueFileResult(enqueued=True, job=EnqueuedJob.from_job(job))


@router.post(
    "/jobs/requeue-failed",
    response_model=BulkRequeueFailedResult,
    status_code=202,
)
def requeue_all_failed_endpoint(
    scheduler: JobScheduler = Depends(get_job_scheduler),
    session: Session = Depends(get_session),
) -> BulkRequeueFailedResult:
    """Requeue every currently-``FAILED`` Job in one call -- "Requeue all failed" (COL-172).

    Wraps :meth:`collapsarr.jobs.scheduler.JobScheduler.requeue_all_failed`,
    the batch counterpart of ``POST /api/jobs/requeue`` above. Unlike that
    per-row action (which always bypasses the Recently-Processed Window),
    this one *respects* it -- a file whose most recent terminal history row
    falls inside the window is skipped, not requeued, since a bulk retry of
    every failed file is closer in spirit to the automatic paths the window
    protects against. A ``202`` is returned whether anything was actually
    requeued or not; the response's ``requeued``/``skipped`` split reports
    exactly what happened to every currently-failed file, so an all-skipped
    pass is a valid outcome, not an error or a silent partial success.
    """
    outcome = scheduler.requeue_all_failed(session=session)
    return BulkRequeueFailedResult(
        requeued=[EnqueuedJob.from_job(job) for job in outcome.requeued],
        skipped=outcome.skipped,
    )


@router.post(
    "/jobs/trigger-default-audio",
    response_model=SetDefaultAudioTriggerResult,
    status_code=202,
)
def manual_set_default_audio_trigger_endpoint(
    body: SetDefaultAudioTriggerRequest,
    scheduler: JobScheduler = Depends(get_job_scheduler),
) -> SetDefaultAudioTriggerResult:
    """Manually enqueue a ``SET_DEFAULT_AUDIO`` job for one file (COL-155).

    Wraps :meth:`collapsarr.jobs.scheduler.JobScheduler.trigger_set_default_audio`:
    probes the file, resolves whether its Default Audio Track disposition
    needs to change against the persisted preference, and enqueues a job
    only when it does. A ``202`` is returned whether or not a job was
    enqueued; the ``enqueued`` flag distinguishes the two (a skipped file --
    no preference configured, duplicate, unprobeable, or already correct --
    is not an error).

    Always passes ``bypass_dedup_window=True`` (COL-206), matching ``POST
    /api/jobs/trigger``'s "Trigger downmix" behavior: an explicit single-file
    "Set Default Audio Track" click is a deliberate request that overrides
    the Recently-Processed Window cooldown, so it is never silently no-op'd
    by an unrelated prior job (e.g. a ``DOWNMIX`` job) on the same file. The
    window is the only thing bypassed; the "does this file need anything"
    gate is unchanged, so a file that's already correct is still skipped.
    The bulk endpoint below is unaffected -- it still respects the window.
    """
    outcome = scheduler.trigger_set_default_audio(body.file_path, bypass_dedup_window=True)
    if outcome.job is None:
        return SetDefaultAudioTriggerResult(
            enqueued=False, job=None, skip_reason=outcome.skip_reason
        )
    return SetDefaultAudioTriggerResult(enqueued=True, job=EnqueuedJob.from_job(outcome.job))


@router.post(
    "/jobs/trigger-default-audio/bulk",
    response_model=BulkSetDefaultAudioTriggerResult,
    status_code=202,
)
def bulk_set_default_audio_trigger_endpoint(
    body: BulkSetDefaultAudioTriggerRequest,
    scheduler: JobScheduler = Depends(get_job_scheduler),
    session: Session = Depends(get_session),
) -> BulkSetDefaultAudioTriggerResult:
    """Manually enqueue ``SET_DEFAULT_AUDIO`` jobs for one or more Library selections (COL-156).

    ``body.references`` is resolved and cascaded down to its unique set of
    file-bearing leaves by :func:`_resolve_default_audio_leaf_files` -- see
    there for the cascade/de-dup/bridge algorithm, which mirrors
    ``POST /api/library/tracked``'s bulk Tracked-update endpoint
    (:mod:`collapsarr.library.routes`, COL-101). ``404``/``422`` from that
    resolution (an unknown ``node_id``, or a ``node_type`` that doesn't match
    the referenced node's actual kind) propagate as this endpoint's own
    response, same as the Tracked-update endpoint.

    :meth:`~collapsarr.jobs.scheduler.JobScheduler.trigger_set_default_audio`
    is then called once per resolved file, against the current global
    Preferred Default Audio setting -- there is no per-call override, unlike
    ``POST /api/jobs/trigger``'s ``extra_languages``. A ``202`` is returned
    whether any individual file was enqueued or skipped; each result's own
    ``enqueued`` flag distinguishes the two, mirroring the single-file
    endpoint's skip semantics per file.
    """
    file_paths = _resolve_default_audio_leaf_files(session, body.references)

    results: list[FileSetDefaultAudioResult] = []
    for file_path in file_paths:
        outcome = scheduler.trigger_set_default_audio(file_path, session=session)
        if outcome.job is None:
            results.append(
                FileSetDefaultAudioResult(
                    file_path=file_path,
                    enqueued=False,
                    job=None,
                    skip_reason=outcome.skip_reason,
                )
            )
        else:
            results.append(
                FileSetDefaultAudioResult(
                    file_path=file_path, enqueued=True, job=EnqueuedJob.from_job(outcome.job)
                )
            )

    return BulkSetDefaultAudioTriggerResult(results=results)


@router.delete("/jobs/{job_id}", response_model=CancelJobResult)
def cancel_job_endpoint(
    job_id: str,
    scheduler: JobScheduler = Depends(get_job_scheduler),
    session: Session = Depends(get_session),
) -> CancelJobResult:
    """Cancel one still-``PENDING`` Job by id (COL-168).

    Wraps :meth:`~collapsarr.jobs.scheduler.JobScheduler.cancel_job` --
    see there for the get/cancel/delete-history sequence and its
    ``None``/``False``/``True`` result contract. A ``job_id`` that isn't a
    valid UUID can't name any job at all, so it's folded into the same
    ``404`` :meth:`~collapsarr.jobs.scheduler.JobScheduler.cancel_job`
    reports for an unknown one, without calling it.
    """
    try:
        job_uuid = UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"No such job: {job_id!r}") from None

    outcome = scheduler.cancel_job(job_uuid, session=session)
    if outcome is None:
        raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
    return CancelJobResult(cancelled=outcome)


# --- POST /api/jobs/clear (COL-173) -------------------------------------------


@router.post("/jobs/clear", response_model=ClearQueueResult, status_code=202)
def clear_queue_endpoint(
    scheduler: JobScheduler = Depends(get_job_scheduler),
    session: Session = Depends(get_session),
) -> ClearQueueResult:
    """Cancel every currently-``PENDING`` Job in one call -- "Clear queue" (COL-173).

    Wraps :meth:`collapsarr.jobs.scheduler.JobScheduler.clear_queue`, the
    batch counterpart of ``DELETE /api/jobs/{job_id}`` above. Every Job that
    is ``PENDING`` at the moment this pass starts is cancelled unless a
    worker claims it first (the queue keeps running concurrently -- there is
    no push mechanism to freeze it mid-request, only polling); the
    response's ``cancelled``/``already_running`` split reports exactly what
    happened to that snapshot, so a race is surfaced rather than silently
    swallowed. Because cancelling frees a slot exactly like any other
    cancellation, the Auto-Queue Limit's top-up (COL-171) runs once,
    immediately after the whole batch -- clearing the queue is not a pause,
    it simply resets to whatever the scanner refills next. A ``202`` is
    returned even when the queue was already empty (``cancelled=0``,
    ``already_running=0`` is a valid outcome, not an error).
    """
    outcome = scheduler.clear_queue(session=session)
    return ClearQueueResult(cancelled=outcome.cancelled, already_running=outcome.already_running)


@router.post("/jobs/{job_id}/bump", response_model=BumpJobResult)
def bump_job_endpoint(
    job_id: str,
    scheduler: JobScheduler = Depends(get_job_scheduler),
) -> BumpJobResult:
    """Bump one still-``PENDING`` Job to the front of the queue by id (COL-169).

    Wraps :meth:`~collapsarr.jobs.scheduler.JobScheduler.bump_job_to_front` --
    see there for the ``None``/``True``/``False`` result contract. A
    ``job_id`` that isn't a valid UUID can't name any job at all, so it's
    folded into the same ``404``
    :meth:`~collapsarr.jobs.scheduler.JobScheduler.bump_job_to_front` reports
    for an unknown one, without calling it -- mirroring
    :func:`cancel_job_endpoint`.
    """
    try:
        job_uuid = UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"No such job: {job_id!r}") from None

    outcome = scheduler.bump_job_to_front(job_uuid)
    if outcome is None:
        raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
    return BumpJobResult(bumped=outcome)


# --- POST /api/jobs/process-now (COL-229) -------------------------------------


@router.post("/jobs/process-now", response_model=ProcessNowResult, status_code=202)
def process_now_endpoint(
    body: ProcessNowRequest,
    scheduler: JobScheduler = Depends(get_job_scheduler),
) -> ProcessNowResult:
    """Force-start a downmix Job for one file immediately -- "Process Now" (COL-229).

    Checks :meth:`~collapsarr.jobs.scheduler.JobScheduler.
    would_exceed_concurrency_limit` *first*, before touching the scheduler's
    :meth:`~collapsarr.jobs.scheduler.JobScheduler.process_now` at all: when
    it reports ``True`` and the request didn't already set ``confirm=True``,
    this returns immediately with ``needs_confirmation=True`` (``enqueued``
    ``False``, ``job`` ``null``) -- nothing is created or started, so a
    caller that never re-submits with ``confirm=True`` has left the file
    exactly as it was (``CONTEXT.md``'s **Process Now** entry: "prompting for
    confirmation if starting it would exceed the configured limit").

    Otherwise -- under the limit, or already confirmed -- delegates to
    :meth:`~collapsarr.jobs.scheduler.JobScheduler.process_now`: force-starts
    an already-``PENDING``/``RUNNING`` Job for the file, or creates one first
    if none exists yet, bypassing both Auto-Processing Pause and the
    Concurrency Limit either way. A ``202`` is returned whether or not a Job
    was force-started; ``enqueued`` distinguishes the two (a skipped file --
    unprobeable, or no qualifying target -- is not an error).
    """
    if not body.confirm and scheduler.would_exceed_concurrency_limit():
        return ProcessNowResult(enqueued=False, job=None, needs_confirmation=True)

    job = scheduler.process_now(body.file_path)
    if job is None:
        return ProcessNowResult(enqueued=False, job=None)
    return ProcessNowResult(enqueued=True, job=EnqueuedJob.from_job(job))
