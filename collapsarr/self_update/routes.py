"""HTTP status/liveness endpoint for the Self-Update state (COL-230).

Thin layer over :mod:`collapsarr.self_update.service`, exposed as a FastAPI
:class:`~fastapi.APIRouter` mounted under ``/api/system`` by
:func:`collapsarr.main.create_app` -- same prefix, same auth gate (every
route under ``/api`` inherits the session/API-key middleware; see
:mod:`collapsarr.auth.enforcement`) as :mod:`collapsarr.update_check.routes`/
:mod:`collapsarr.ffmpeg_download.routes`, which this module deliberately
mirrors in shape. No bespoke auth wiring needed here.

Endpoint:

* ``GET /api/system/self-update/status`` -- the current self-update phase and
  in-progress guard state, for the frontend's future polling screen (COL-234+)
  to consume. This ticket only exposes the read side -- there is no
  ``POST .../start`` endpoint yet; that lands with the apply flow (COL-232).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_session
from .service import get_self_update_state

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


__all__ = ["SelfUpdateStatusRead", "router"]
