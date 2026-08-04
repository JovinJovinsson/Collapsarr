"""HTTP REST endpoints for the persisted Update Check state (COL-87, COL-89).

Thin layer over :mod:`collapsarr.update_check.service`, exposed as a FastAPI
:class:`~fastapi.APIRouter` mounted under ``/api/system`` by
:func:`collapsarr.main.create_app` -- same prefix, same auth gate (every route
under ``/api`` inherits the session/API-key middleware; see
:mod:`collapsarr.auth.enforcement`) as :mod:`collapsarr.health.routes`, which
this module deliberately mirrors in shape. Unlike the Health Check Framework,
there is exactly one singleton state row (no per-Check-Key id), so dismiss/
undismiss (COL-89) take no id parameter either -- they always act on that one
row.

Endpoints:

* ``GET /api/system/updates`` -- the running instance's version, the latest
  known release for the configured channel (or ``None`` before the first
  tick), when that data was last refreshed, whether an update is available,
  and whether this instance is running under Docker (``is_docker``, COL-90 --
  detected via :func:`~collapsarr.update_check.environment.
  is_docker_environment`, so the frontend can render the matching install
  instructions without any detection logic of its own).
* ``POST /api/system/updates/recheck`` -- runs the Update Check scheduler's
  tick synchronously (mirrors :meth:`collapsarr.health.scheduler.
  HealthCheckScheduler.run_once` via ``POST /api/system/health-checks/
  recheck``) and returns the refreshed state, without disturbing the
  background scheduler's own periodic timer/thread. Reuses the exact same
  reconciliation path as a scheduled tick, so a manual recheck fires the same
  edge-triggered notification a scheduled tick would.
* ``POST /api/system/updates/dismiss`` -- dismisses the current "update
  available" notice (COL-89): ``404`` if no tick has ever run, ``409`` if the
  running version is already up to date (nothing to dismiss).
* ``POST /api/system/updates/undismiss`` -- clears a dismissal (COL-89):
  ``404`` if no tick has ever run; otherwise always succeeds.

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
from .environment import is_docker_environment
from .scheduler import UpdateCheckScheduler
from .service import (
    UpdateCheckStateNotFoundError,
    UpdateNotAvailableError,
    dismiss_update_check,
    get_update_check_state,
    is_update_available,
    undismiss_update_check,
)

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
    :func:`~collapsarr.update_check.service.is_update_available` rather than
    stored -- both would go stale the instant a new version shipped if
    persisted instead of derived on every read. ``latest_version`` is the
    fetched release's ``tag_name`` (``UpdateCheckState.latest_tag``) -- the
    same identity ``running_version`` is compared against; ``latest_version_label``
    carries the release's human-readable name (``UpdateCheckState.
    latest_version_label``) for display, when it differs from the tag.
    Every ``latest_*``/``changelog``/``checked_at`` field is ``None`` before
    the very first tick has run. ``dismissed_at`` (COL-89) is ``None`` unless
    an operator has dismissed the current "update available" notice; it is
    automatically cleared the next time ``latest_tag`` changes (see
    :func:`~collapsarr.update_check.service.reconcile_update_check`).
    ``is_docker`` (COL-90) is computed fresh on every read via
    :func:`~collapsarr.update_check.environment.is_docker_environment` --
    process-global and independent of the persisted row, unlike every other
    field here.
    """

    running_version: str
    latest_version: str | None
    latest_version_label: str | None
    changelog: str | None
    checked_at: datetime | None
    update_available: bool
    dismissed_at: datetime | None
    is_docker: bool


# --- helpers -------------------------------------------------------------


def _read_state(session: Session) -> UpdateCheckStateRead:
    """Build the current :class:`UpdateCheckStateRead` from persisted state.

    The single seam both endpoints below call, so ``GET`` and the post-recheck
    response are built identically. ``state`` is ``None`` only if the app's
    startup tick (:func:`collapsarr.main.create_app`'s lifespan) never ran.

    ``update_available`` is delegated to :func:`~collapsarr.update_check.
    service.is_update_available` -- the same seam :func:`~collapsarr.
    update_check.service.dismiss_update_check`'s "nothing to dismiss" guard
    uses, so this endpoint's reported value and that guard's decision can
    never drift on which comparison function (stable vs. beta, COL-88)
    applies for a given row, nor on the "unknown reports as available"
    handling of a ``None`` row/``latest_tag``.

    ``is_docker`` (COL-90) is unrelated to the persisted row entirely -- it's
    a fresh :func:`~collapsarr.update_check.environment.is_docker_environment`
    call every time, referenced unqualified so tests can monkeypatch
    ``collapsarr.update_check.routes.is_docker_environment`` the same way
    they already monkeypatch ``__version__`` above.
    """
    state = get_update_check_state(session)
    return UpdateCheckStateRead(
        running_version=__version__,
        latest_version=state.latest_tag if state is not None else None,
        latest_version_label=state.latest_version_label if state is not None else None,
        changelog=state.changelog if state is not None else None,
        checked_at=state.checked_at if state is not None else None,
        update_available=is_update_available(state, __version__),
        dismissed_at=state.dismissed_at if state is not None else None,
        is_docker=is_docker_environment(),
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


@router.post("/updates/dismiss", response_model=UpdateCheckStateRead)
def dismiss_updates_endpoint(
    session: Session = Depends(get_session),
) -> UpdateCheckStateRead:
    """Dismiss the current "update available" notice (COL-89).

    Idempotent -- dismissing an already-dismissed notice just refreshes the
    timestamp. ``404`` if no Update Check tick has ever run (nothing to
    dismiss yet); in practice this only happens if the app's startup tick
    never ran, since the lifespan always runs one synchronously. ``409`` if
    the running version is already up to date -- there is no current "update
    available" occurrence to dismiss, mirroring
    :func:`collapsarr.health.routes.dismiss_health_check_endpoint`'s handling
    of ``HealthCheckNotFailingError``.
    """
    try:
        dismiss_update_check(session)
    except UpdateCheckStateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UpdateNotAvailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _read_state(session)


@router.post("/updates/undismiss", response_model=UpdateCheckStateRead)
def undismiss_updates_endpoint(
    session: Session = Depends(get_session),
) -> UpdateCheckStateRead:
    """Clear a dismissal on the current "update available" notice early (COL-89).

    Idempotent -- undismissing a notice that isn't dismissed is a no-op.
    ``404`` if no Update Check tick has ever run.
    """
    try:
        undismiss_update_check(session)
    except UpdateCheckStateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _read_state(session)


__all__ = ["UpdateCheckStateRead", "get_update_check_scheduler", "router"]
