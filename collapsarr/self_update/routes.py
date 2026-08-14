"""HTTP endpoints for the Self-Update state and pipx apply flow (COL-230, COL-232, COL-233).

Thin layer over :mod:`collapsarr.self_update.service`/:mod:`collapsarr.
self_update.apply`, exposed as a FastAPI :class:`~fastapi.APIRouter` mounted
under ``/api/system`` by :func:`collapsarr.main.create_app` -- same prefix,
same auth gate (every route under ``/api`` inherits the session/API-key
middleware; see :mod:`collapsarr.auth.enforcement`) as
:mod:`collapsarr.update_check.routes`/:mod:`collapsarr.ffmpeg_download.routes`,
which this module deliberately mirrors in shape. No bespoke auth wiring
needed here.

Endpoints:

* ``POST /api/system/self-update/apply`` (COL-232, COL-233, COL-235) --
  triggers the apply flow for this install's method: ``pipx``
  (:func:`~collapsarr.self_update.apply.apply_pipx_update`/:func:`~collapsarr.
  self_update.apply.apply_with_flow` -- download + SHA-256-verify, ``pipx
  upgrade collapsarr``, re-exec) or ``native``
  (:func:`~collapsarr.self_update.native.apply_native_update` -- verify into a
  staging dir, spawn the finish-update handoff, exit; the handoff then swaps
  and re-execs). ``403`` when this install's
  :func:`~collapsarr.system.info.install_method` is neither (a Docker install
  upgrades by pulling a new image, mirroring
  :mod:`collapsarr.ffmpeg_download.routes`'s Docker-gating precedent);
  ``409`` when no newer *stable* release is available to apply
  (:func:`~collapsarr.self_update.apply.stable_update_target`), when an
  attempt is already in progress
  (:class:`~collapsarr.self_update.service.SelfUpdateAlreadyInProgressError`,
  mirroring :mod:`collapsarr.backup.routes`'s ``BackupUnavailableError``
  ``409`` precedent), or when Jobs are currently running and the request
  body omitted ``flow`` (COL-233 -- see below); ``502`` on a downstream
  failure (checksum mismatch, network error, a failed ``pipx upgrade``, a
  staging/extraction failure, or an in-flight-Job-handling failure) with that
  failure's own human-readable error as the response ``detail``, mirroring
  :mod:`collapsarr.ffmpeg_download.routes`'s failure-reporting shape.

  **In-flight Jobs (COL-233).** The request body's optional ``flow`` field
  (:class:`SelfUpdateApplyRequest`) picks how to handle downmix Jobs
  currently ``RUNNING`` at trigger time: ``"cancel_and_restart"`` hard-kills
  and immediately requeues them (bypassing the Recently-Processed Window);
  ``"wait_and_restart"`` blocks until they finish naturally, cancelling
  nothing (:func:`~collapsarr.self_update.apply.apply_with_flow`). Omitting
  ``flow`` preserves the original COL-232 behaviour *only* when nothing is
  currently running -- with Jobs running and no ``flow`` chosen, this
  endpoint refuses with ``409`` rather than guessing which flow the operator
  wants. **Scope note (COL-233/COL-235 merge):** ``apply_with_flow`` only
  wraps the ``pipx`` apply path today -- a ``native`` install with Jobs
  running and an explicit ``flow`` also gets a ``409`` (flow-based Job
  handling isn't yet wired into the native staged-handoff path); extending
  it is left as follow-up work, not implemented here.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from .. import __version__
from ..database import get_session
from ..jobs.queue import JobQueue
from ..jobs.scheduler import JobScheduler
from ..system.info import INSTALL_METHOD_NATIVE, INSTALL_METHOD_PIPX, install_method
from ..update_check.service import get_update_check_state
from .apply import (
    ReexecFn,
    SubprocessRunner,
    apply_pipx_update,
    apply_with_flow,
    stable_update_target,
)
from .native import ExitFn, HandoffSpawner, apply_native_update
from .service import SelfUpdateAlreadyInProgressError, get_self_update_state

router = APIRouter(prefix="/api/system/self-update", tags=["system"])


# --- schemas -----------------------------------------------------------------


class SelfUpdateStatusRead(BaseModel):
    """Response shape for ``GET /api/system/self-update/status``.

    Field-for-field off :class:`~collapsarr.self_update.models.SelfUpdateState`
    -- ``in_progress``/``phase``/``previous_version`` are read straight from
    the persisted row, get-or-created on first read (see
    :func:`~collapsarr.self_update.service.get_self_update_state`), so this
    always returns ``in_progress=False``, ``phase="idle"``,
    ``previous_version=None`` before any self-update flow has ever run.
    """

    in_progress: bool
    phase: str
    previous_version: str | None


class SelfUpdateApplyRequest(BaseModel):
    """Request body for ``POST /api/system/self-update/apply`` (COL-233).

    Entirely optional: an app whose Job Queue currently has nothing
    ``RUNNING`` needs no ``flow`` at all -- the endpoint proceeds through the
    original COL-232 "no Jobs running" path exactly as it did before this
    field existed. ``flow`` only becomes *required* once at least one Job is
    currently ``RUNNING`` (the endpoint returns ``409`` rather than guessing):

    * ``"cancel_and_restart"`` -- hard-cancel every ``RUNNING`` Job (logged
      as a failure, same as any other manual cancel, COL-192) and
      immediately requeue each one, bypassing the Recently-Processed Window.
    * ``"wait_and_restart"`` -- block until nothing is ``RUNNING`` anymore,
      cancelling nothing.

    See :func:`~collapsarr.self_update.apply.apply_with_flow` for the full
    contract, including the Auto-Processing Pause stash/force-pause/restore
    bookkeeping both flows trigger.
    """

    model_config = ConfigDict(extra="forbid")

    flow: Literal["cancel_and_restart", "wait_and_restart"] | None = None


class SelfUpdateApplyRead(BaseModel):
    """Response shape for a successful ``POST /api/system/self-update/apply``.

    ``ok`` is only ever observed as ``True`` in a test that injects a fake
    ``reexec_fn`` (see :mod:`collapsarr.self_update.apply`'s module
    docstring) -- a real re-exec replaces this process outright, so no
    production HTTP response is ever actually sent for a successful apply.
    """

    ok: bool


# --- endpoints ---------------------------------------------------------------


@router.get("/status", response_model=SelfUpdateStatusRead)
def get_self_update_status_endpoint(
    session: Session = Depends(get_session),
) -> SelfUpdateStatusRead:
    """Return the current self-update phase and in-progress guard state."""
    state = get_self_update_state(session)
    return SelfUpdateStatusRead(
        in_progress=state.in_progress,
        phase=state.phase,
        previous_version=state.previous_version,
    )


@router.post("/apply", response_model=SelfUpdateApplyRead)
def apply_self_update_endpoint(
    request: Request,
    body: SelfUpdateApplyRequest | None = None,
    session: Session = Depends(get_session),
) -> SelfUpdateApplyRead:
    """Trigger the self-update apply flow: verify, install, re-exec (COL-232, COL-233, COL-235).

    Dispatches on this install's :func:`~collapsarr.system.info.install_method`:
    ``pipx`` runs :func:`~collapsarr.self_update.apply.apply_pipx_update` (no
    Jobs running) or :func:`~collapsarr.self_update.apply.apply_with_flow`
    (Jobs running, COL-233) -- verify + ``pipx upgrade`` + re-exec, optionally
    wrapped with in-flight-Job handling; ``native`` runs
    :func:`~collapsarr.self_update.native.apply_native_update` (verify into a
    staging dir + spawn the finish-update handoff + exit, which then swaps and
    re-execs -- COL-233's Job-handling wrapper isn't wired into this path yet,
    see the scope note below). ``403`` when the method is neither (a Docker
    install upgrades by pulling a new image, not through this endpoint);
    ``409`` when no newer stable release is available, an attempt is already
    in progress, or Jobs are currently running with no ``flow`` chosen
    (COL-233); ``502`` on a checksum mismatch, network failure, failed ``pipx
    upgrade``, a staging/extraction failure, or an in-flight-Job-handling
    failure -- see the module docstring for the full gating/error-mapping
    contract. The ``self_update_*`` ``app.state`` seams (transport, and the
    pipx subprocess/re-exec or native handoff-spawn/exit doubles) let tests
    inject fakes instead of a real network call, subprocess, ``os.execv``,
    process spawn, or process exit; production leaves them unset.

    ``body.flow`` (COL-233) is optional. ``None`` (the default) preserves the
    original COL-232 behaviour, but *only* when the Job Queue currently has
    nothing ``RUNNING`` -- checked via ``app.state.job_queue`` (wired only
    when :func:`collapsarr.main.create_app` is built with
    ``enable_scheduler=True``; absent, e.g. in a lightweight test app, this
    reads as "nothing running"). With Jobs running and no ``flow``, this
    endpoint refuses (``409``) rather than silently picking a flow on the
    operator's behalf. A non-``None`` ``flow`` always routes through
    :func:`~collapsarr.self_update.apply.apply_with_flow` instead of
    :func:`~collapsarr.self_update.apply.apply_pipx_update` directly, even if
    it turns out nothing is running by the time it runs -- that function
    handles an empty Job Queue for either flow just as correctly (an
    immediate no-op wait/cancel pass), so there is no need for this endpoint
    to duplicate the "is anything actually running" check for that branch.
    **Scope note:** a non-``None`` ``flow`` on a ``native`` install is a
    ``409`` today (see module docstring) -- ``apply_with_flow`` only wraps the
    ``pipx`` path, a follow-up ticket's concern, not implemented here.
    """
    method = install_method()
    if method not in (INSTALL_METHOD_PIPX, INSTALL_METHOD_NATIVE):
        raise HTTPException(
            status_code=403,
            detail="Self-update apply is only available for pipx and native installs.",
        )

    update_state = get_update_check_state(session)
    target_tag = stable_update_target(update_state, __version__)
    if target_tag is None:
        raise HTTPException(
            status_code=409,
            detail="No newer stable release is available to apply.",
        )

    transport = getattr(request.app.state, "self_update_transport", None)

    flow = body.flow if body is not None else None
    job_queue: JobQueue | None = getattr(request.app.state, "job_queue", None)
    job_scheduler: JobScheduler | None = getattr(request.app.state, "job_scheduler", None)
    running_count = job_queue.count_running() if job_queue is not None else 0

    if flow is None and running_count > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                "Jobs are currently running; choose a flow "
                "('cancel_and_restart' or 'wait_and_restart') to proceed."
            ),
        )
    if flow is not None and method == INSTALL_METHOD_NATIVE:
        raise HTTPException(
            status_code=409,
            detail=(
                "In-flight Job handling ('flow') is not yet supported for native "
                "installs; wait until no Jobs are running before applying."
            ),
        )

    try:
        if method == INSTALL_METHOD_NATIVE:
            spawn_handoff: HandoffSpawner | None = getattr(
                request.app.state, "self_update_handoff_spawner", None
            )
            exit_fn: ExitFn | None = getattr(request.app.state, "self_update_exit_fn", None)
            outcome = apply_native_update(
                session,
                target_tag=target_tag,
                transport=transport,
                spawn_handoff=spawn_handoff,
                exit_fn=exit_fn,
            )
        elif flow is None:
            subprocess_runner: SubprocessRunner | None = getattr(
                request.app.state, "self_update_subprocess_runner", None
            )
            reexec_fn: ReexecFn | None = getattr(
                request.app.state, "self_update_reexec_fn", None
            )
            outcome = apply_pipx_update(
                session,
                target_tag=target_tag,
                transport=transport,
                subprocess_runner=subprocess_runner,
                reexec_fn=reexec_fn,
            )
        else:
            if job_queue is None:
                raise HTTPException(
                    status_code=503,
                    detail="Job queue is not available; cannot process a flow-based apply request.",
                )
            subprocess_runner = getattr(request.app.state, "self_update_subprocess_runner", None)
            reexec_fn = getattr(request.app.state, "self_update_reexec_fn", None)
            outcome = apply_with_flow(
                session,
                target_tag=target_tag,
                flow=flow,
                queue=job_queue,
                scheduler=job_scheduler,
                transport=transport,
                subprocess_runner=subprocess_runner,
                reexec_fn=reexec_fn,
            )
    except SelfUpdateAlreadyInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not outcome.ok:
        raise HTTPException(status_code=502, detail=outcome.error)

    return SelfUpdateApplyRead(ok=True)


__all__ = [
    "SelfUpdateApplyRead",
    "SelfUpdateApplyRequest",
    "SelfUpdateStatusRead",
    "router",
]
