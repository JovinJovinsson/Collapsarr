"""HTTP REST endpoints for the persisted Update Check state (COL-87).

Thin layer over :mod:`collapsarr.update_check.service`, exposed as a FastAPI
:class:`~fastapi.APIRouter` mounted under ``/api/system`` by
:func:`collapsarr.main.create_app` -- same prefix, same auth gate (every route
under ``/api`` inherits the session/API-key middleware; see
:mod:`collapsarr.auth.enforcement`) as :mod:`collapsarr.health.routes`, which
this module deliberately mirrors in shape. Unlike the Health Check Framework,
there is exactly one singleton state row (no per-Check-Key id), so there is no
list endpoint and no dismiss/undismiss action here -- just "read the current
state" and "recheck now".

Endpoints:

* ``GET /api/system/updates`` -- the running instance's version, the latest
  known release for the configured channel (or ``None`` before the first
  tick), when that data was last refreshed, and whether an update is
  available.
* ``POST /api/system/updates/recheck`` -- runs the Update Check scheduler's
  tick synchronously (mirrors :meth:`collapsarr.health.scheduler.
  HealthCheckScheduler.run_once` via ``POST /api/system/health-checks/
  recheck``) and returns the refreshed state, without disturbing the
  background scheduler's own periodic timer/thread.

An "update available" is informational, not a failure state (see
``CONTEXT.md``'s Update Check entry) -- there is no severity/Check Code here,
just a boolean the frontend renders with its own neutral styling, distinct
from the error/warning-oriented ``/health`` banner.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import __version__
from ..database import get_session
from ..settings.models import UPDATE_CHANNEL_BETA
from .comparison import is_up_to_date, is_up_to_date_beta
from .scheduler import UpdateCheckScheduler
from .service import get_update_check_state

router = APIRouter(prefix="/api/system", tags=["system"])


# --- dependencies ------------------------------------------------------------


def get_update_check_scheduler(request: Request) -> UpdateCheckScheduler:
    """Return the app's live :class:`UpdateCheckScheduler`, or ``503`` if none is wired.

    Mirrors :func:`collapsarr.health.routes.get_health_scheduler`: the recheck
    endpoint below drives an out-of-band tick on the very same scheduler
    instance the background loop (or the app-startup synchronous tick) uses,
    exposed on ``app.state.update_check_scheduler`` by
    :func:`collapsarr.main.create_app`'s lifespan. That scheduler is wired
    unconditionally (like ``health_scheduler``, unlike the Job Queue's
    ``job_scheduler``), so this only ever 503s for an app that never ran its
    lifespan at all.
    """
    scheduler: UpdateCheckScheduler | None = getattr(
        request.app.state, "update_check_scheduler", None
    )
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="Update check scheduler is not available.",
        )
    return scheduler


# --- schemas -----------------------------------------------------------------


class UpdateCheckStateRead(BaseModel):
    """Response shape for the singleton Update Check state.

    Distinct from :class:`~collapsarr.update_check.models.UpdateCheckState`
    field-for-field: ``running_version`` is computed from
    :data:`collapsarr.__version__` (not a persisted column), and
    ``update_available`` is computed via
    :func:`~collapsarr.update_check.comparison.is_up_to_date` rather than
    stored -- both would go stale the instant a new version shipped if
    persisted instead of derived on every read. ``latest_version`` is the
    fetched release's ``tag_name`` (``UpdateCheckState.latest_tag``) -- the
    same identity ``running_version`` is compared against; ``latest_version_label``
    carries the release's human-readable name (``UpdateCheckState.
    latest_version_label``) for display, when it differs from the tag.
    Every ``latest_*``/``changelog``/``checked_at`` field is ``None`` before
    the very first tick has run.
    """

    running_version: str
    latest_version: str | None
    latest_version_label: str | None
    changelog: str | None
    checked_at: datetime | None
    update_available: bool


# --- helpers -------------------------------------------------------------


def _read_state(session: Session) -> UpdateCheckStateRead:
    """Build the current :class:`UpdateCheckStateRead` from persisted state.

    The single seam both endpoints below call, so ``GET`` and the post-recheck
    response are built identically. ``state`` is ``None`` only if the app's
    startup tick (:func:`collapsarr.main.create_app`'s lifespan) never ran --
    handled the same way :func:`~collapsarr.update_check.comparison.
    is_up_to_date`/:func:`~collapsarr.update_check.comparison.
    is_up_to_date_beta` treat a ``None`` ``latest_tag``: "unknown" reports as
    an update being available rather than silently claiming the instance is
    current with no evidence.

    The comparison function is picked from ``state.channel`` (COL-88) -- the
    channel *that tick's* fetch actually ran against, recorded on the same
    row as ``latest_tag`` -- rather than re-reading the currently configured
    channel, so ``latest_tag``/``update_available`` always describe the same
    channel consistently even mid-switch (before the next tick/recheck runs
    against the newly selected channel). Falls back to the stable comparison
    when there's no state yet, matching the pre-COL-88 default.
    """
    state = get_update_check_state(session)
    latest_version = state.latest_tag if state is not None else None
    channel = state.channel if state is not None else None
    compare = is_up_to_date_beta if channel == UPDATE_CHANNEL_BETA else is_up_to_date
    return UpdateCheckStateRead(
        running_version=__version__,
        latest_version=latest_version,
        latest_version_label=state.latest_version_label if state is not None else None,
        changelog=state.changelog if state is not None else None,
        checked_at=state.checked_at if state is not None else None,
        update_available=not compare(__version__, latest_version),
    )


# --- endpoints ---------------------------------------------------------------


@router.get("/updates", response_model=UpdateCheckStateRead)
def get_updates_endpoint(
    session: Session = Depends(get_session),
) -> UpdateCheckStateRead:
    """Return the current Update Check state: running vs. latest version, changelog,
    when it was last checked, and whether an update is available."""
    return _read_state(session)


@router.post("/updates/recheck", response_model=UpdateCheckStateRead)
def recheck_updates_endpoint(
    scheduler: UpdateCheckScheduler = Depends(get_update_check_scheduler),
    session: Session = Depends(get_session),
) -> UpdateCheckStateRead:
    """Run an Update Check tick immediately and return the refreshed state (COL-87).

    Calls :meth:`~collapsarr.update_check.scheduler.UpdateCheckScheduler.run_once`
    directly -- the exact seam the periodic background loop's own iteration
    calls -- so this is a genuine extra, out-of-band tick, not a scheduler
    restart: it touches neither the loop's sleep/wake ``threading.Event`` nor
    its ``_thread``, so the next periodic tick still fires
    :data:`~collapsarr.update_check.scheduler.INTERVAL_SECONDS` after whichever
    tick preceded *this* call, entirely unaffected by it (mirrors
    :func:`collapsarr.health.routes.recheck_health_checks_endpoint`).
    """
    scheduler.run_once()
    return _read_state(session)


__all__ = ["UpdateCheckStateRead", "get_update_check_scheduler", "router"]
