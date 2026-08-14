"""HTTP endpoints for the Self-Update state and pipx apply flow (COL-230, COL-232).

Thin layer over :mod:`collapsarr.self_update.service`/:mod:`collapsarr.
self_update.apply`, exposed as a FastAPI :class:`~fastapi.APIRouter` mounted
under ``/api/system`` by :func:`collapsarr.main.create_app` -- same prefix,
same auth gate (every route under ``/api`` inherits the session/API-key
middleware; see :mod:`collapsarr.auth.enforcement`) as
:mod:`collapsarr.update_check.routes`/:mod:`collapsarr.ffmpeg_download.routes`,
which this module deliberately mirrors in shape. No bespoke auth wiring
needed here.

Endpoints:

* ``GET /api/system/self-update/status`` -- the current self-update phase and
  in-progress guard state, for the frontend's future polling screen (COL-234+)
  to consume.
* ``POST /api/system/self-update/apply`` (COL-232, COL-235) -- triggers the
  apply flow for this install's method: ``pipx``
  (:func:`~collapsarr.self_update.apply.apply_pipx_update` -- download +
  SHA-256-verify, ``pipx upgrade collapsarr``, re-exec) or ``native``
  (:func:`~collapsarr.self_update.native.apply_native_update` -- verify into a
  staging dir, spawn the finish-update handoff, exit; the handoff then swaps
  and re-execs). ``403`` when this install's
  :func:`~collapsarr.system.info.install_method` is neither (a Docker install
  upgrades by pulling a new image, mirroring
  :mod:`collapsarr.ffmpeg_download.routes`'s Docker-gating precedent);
  ``409`` when no newer *stable* release is available to apply
  (:func:`~collapsarr.self_update.apply.stable_update_target`) or when an
  attempt is already in progress
  (:class:`~collapsarr.self_update.service.SelfUpdateAlreadyInProgressError`,
  mirroring :mod:`collapsarr.backup.routes`'s ``BackupUnavailableError``
  ``409`` precedent); ``502`` on a downstream failure (checksum mismatch,
  network error, or a failed ``pipx upgrade``) with that failure's own
  human-readable error as the response ``detail``, mirroring
  :mod:`collapsarr.ffmpeg_download.routes`'s failure-reporting shape. Scoped
  to the "no Jobs running" case only -- see
  :mod:`collapsarr.self_update.apply`'s module docstring.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import __version__
from ..database import get_session
from ..system.info import INSTALL_METHOD_NATIVE, INSTALL_METHOD_PIPX, install_method
from ..update_check.service import get_update_check_state
from .apply import ReexecFn, SubprocessRunner, apply_pipx_update, stable_update_target
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
    session: Session = Depends(get_session),
) -> SelfUpdateApplyRead:
    """Trigger the self-update apply flow: verify, install, re-exec (COL-232, COL-235).

    Dispatches on this install's :func:`~collapsarr.system.info.install_method`:
    ``pipx`` runs :func:`~collapsarr.self_update.apply.apply_pipx_update`
    (verify + ``pipx upgrade`` + re-exec); ``native`` runs
    :func:`~collapsarr.self_update.native.apply_native_update` (verify into a
    staging dir + spawn the finish-update handoff + exit, which then swaps and
    re-execs). ``403`` when the method is neither (a Docker install upgrades by
    pulling a new image, not through this endpoint); ``409`` when no newer
    stable release is available or an attempt is already in progress; ``502``
    on a checksum mismatch, network failure, failed ``pipx upgrade``, or a
    staging/extraction failure -- see the module docstring for the full
    gating/error-mapping contract. The ``self_update_*`` ``app.state`` seams
    (transport, and the pipx subprocess/re-exec or native handoff-spawn/exit
    doubles) let tests inject fakes instead of a real network call, subprocess,
    ``os.execv``, process spawn, or process exit; production leaves them unset.
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
        else:
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
    except SelfUpdateAlreadyInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not outcome.ok:
        raise HTTPException(status_code=502, detail=outcome.error)

    return SelfUpdateApplyRead(ok=True)


__all__ = ["SelfUpdateApplyRead", "SelfUpdateStatusRead", "router"]
