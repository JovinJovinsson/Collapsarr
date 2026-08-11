"""HTTP REST endpoints for job history & on-demand triggers (COL-29).

Thin layer wrapping the Job Queue & Scheduler epic's service surface, exposed as
a FastAPI :class:`~fastapi.APIRouter` mounted under ``/api`` by
:func:`collapsarr.main.create_app`. Because everything under ``/api`` is gated by
the API-key middleware (COL-26), every route here inherits key-based auth -- no
per-route auth wiring is needed.

Six endpoints, each wrapping an existing service without adding new job logic:

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
- ``POST /api/jobs/trigger-default-audio`` -- manually enqueues a
  ``SET_DEFAULT_AUDIO`` job for one specific file
  (:meth:`collapsarr.jobs.scheduler.JobScheduler.trigger_set_default_audio`,
  COL-155), mirroring ``POST /api/jobs/trigger``'s shape: same request
  (a bare ``file_path``), same response shape (``enqueued`` + the job, or
  ``enqueued=False``/``job=null`` when the file needs no change).
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
  ``trigger``'s ``extra_languages``.
- ``DELETE /api/jobs/{job_id}`` -- cancels one specific still-``PENDING``
  Job (COL-168) (:meth:`collapsarr.jobs.scheduler.JobScheduler.cancel_job`).
  "Cancel" is deletion, not a new status: on success the Job's persisted
  :class:`~collapsarr.jobs.models.JobHistory` row is deleted too -- a
  cancelled Job leaves no trace, no audit row, no cooldown interaction with
  the Recently-Processed Window. Because the queue's worker pool keeps
  running concurrently, the Job may already have been claimed (or already
  finished) by the time the request lands; that is reported back distinctly
  (see :class:`CancelJobResult`) rather than erroring or silently pretending
  success. A ``job_id`` not present in the live queue at all -- unknown,
  malformed, or already gone -- is a ``404``.

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
from .history import list_job_history
from .models import JobHistory
from .queue import Job, JobKind, JobStatus
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
    kind: JobKind
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
    """Response for ``POST /api/jobs/trigger-default-audio`` (COL-155).

    ``enqueued`` is ``True`` with the created ``job`` when a
    ``SET_DEFAULT_AUDIO`` job was queued. It is ``False`` with ``job`` ``null``
    when the file was skipped -- no Default Audio Track preference is
    configured, a duplicate (already queued / recently processed), unprobeable,
    or the file already carries the correct disposition -- mirroring
    :meth:`collapsarr.jobs.scheduler.JobScheduler.trigger_set_default_audio`
    returning ``None``.
    """

    enqueued: bool
    job: EnqueuedJob | None


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


class FileSetDefaultAudioResult(BaseModel):
    """One resolved file's outcome within a bulk trigger response (COL-156).

    Mirrors :class:`SetDefaultAudioTriggerResult`'s ``enqueued``/``job``
    pair, per file, plus the ``file_path`` identifying which resolved file
    this result belongs to (the bulk response has no other way to attribute
    an outcome back to a specific file).
    """

    file_path: str
    enqueued: bool
    job: EnqueuedJob | None


class BulkSetDefaultAudioTriggerResult(BaseModel):
    """Response for ``POST /api/jobs/trigger-default-audio/bulk`` (COL-156).

    One :class:`FileSetDefaultAudioResult` per unique file the request's
    references resolved to -- see
    :func:`_resolve_default_audio_leaf_files` for how references cascade and
    de-duplicate down to that file set.
    """

    results: list[FileSetDefaultAudioResult]


class CancelJobResult(BaseModel):
    """Response for ``DELETE /api/jobs/{job_id}`` (COL-168).

    ``cancelled`` is ``True`` when the Job was still ``PENDING`` and has now
    been removed from the live queue *and* had its
    :class:`~collapsarr.jobs.models.JobHistory` row deleted -- it leaves no
    trace at all. It is ``False`` -- not an error -- when the Job still
    exists but is no longer ``PENDING`` (a worker already claimed it, or it
    has already reached a terminal status): "too late" to cancel, distinct
    from both success and a generic failure; the already-claimed/finished
    Job runs (or has run) to completion normally, history row intact. A
    ``job_id`` not present in the live queue at all -- unknown, malformed,
    or already gone -- is reported as ``404``, not this shape (there is
    nothing to act on either way).
    """

    cancelled: bool


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
    """
    job = scheduler.trigger_set_default_audio(body.file_path)
    if job is None:
        return SetDefaultAudioTriggerResult(enqueued=False, job=None)
    return SetDefaultAudioTriggerResult(enqueued=True, job=EnqueuedJob.from_job(job))


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
        job = scheduler.trigger_set_default_audio(file_path, session=session)
        if job is None:
            results.append(FileSetDefaultAudioResult(file_path=file_path, enqueued=False, job=None))
        else:
            results.append(
                FileSetDefaultAudioResult(
                    file_path=file_path, enqueued=True, job=EnqueuedJob.from_job(job)
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
