"""HTTP REST endpoints for the full per-check health state (COL-76, COL-82, COL-83).

Thin layer over :mod:`collapsarr.health.service`, exposed as a FastAPI
:class:`~fastapi.APIRouter` mounted under ``/api/system`` by
:func:`collapsarr.main.create_app`. Because everything under ``/api`` is
gated by the auth middleware (session cookie or API key; see
:mod:`collapsarr.auth.enforcement`), the routes here inherit that gate -- no
per-route auth wiring is needed. This is distinct from the unauthenticated
``/health`` liveness probe (:mod:`collapsarr.main`), which only ever surfaces
*currently-failing, non-dismissed* checks with a minimal
``{code, message, severity}`` shape for the app-wide banner; the list endpoint
here returns every registered check's full, current-tick detail (passing or
failing, dismissed or not) for the System > Health page.

Endpoints:

* ``GET /api/system/health-checks`` -- lists every persisted
  :class:`~collapsarr.health.models.HealthCheckState` row (one per *Check Key*
  -- ``(code, instance_id)``, see ``CONTEXT.md``), ordered by code then
  instance. Generic over whatever checks are registered
  (:func:`~collapsarr.health.registry.default_health_checks`) -- nothing here
  is hardcoded to FFmpeg.
* ``POST /api/system/health-checks/{id}/dismiss`` -- dismisses one row (COL-82):
  ``404`` for an unknown id, ``409`` if the check is not currently failing.
* ``POST /api/system/health-checks/{id}/undismiss`` -- clears a dismissal
  (COL-82): ``404`` for an unknown id; otherwise always succeeds, whatever the
  check's current status.
* ``POST /api/system/health-checks/recheck`` -- runs every registered check
  immediately (COL-83) and returns the resulting full state, so an operator
  can confirm a fix without waiting for the next scheduled tick.

Both dismiss/undismiss actions are scoped to one persisted row's ``id`` (its
stable database primary key, returned by the list endpoint precisely so a
later slice could address one check state directly) -- which is already the
Check Key disambiguator for a per-instance check, so no separate instance
parameter is needed.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_session
from .models import HealthCheckState
from .scheduler import HealthCheckScheduler
from .service import (
    HealthCheckNotFailingError,
    HealthCheckStateNotFoundError,
    dismiss_health_check,
    list_health_check_states,
    undismiss_health_check,
)

router = APIRouter(prefix="/api/system", tags=["system"])


# --- dependencies ------------------------------------------------------------


def get_health_scheduler(request: Request) -> HealthCheckScheduler:
    """Return the app's live :class:`HealthCheckScheduler`, or ``503`` if none is wired.

    The recheck endpoint below drives an out-of-band tick on the very same
    scheduler instance the background loop (or the app-startup synchronous
    tick) uses, exposed on ``app.state.health_scheduler`` by
    :func:`collapsarr.main.create_app`'s lifespan. That scheduler is wired
    unconditionally (unlike the Job Queue's ``job_scheduler``, gated behind
    ``enable_scheduler``), so this only ever 503s for an app that never ran
    its lifespan at all -- mirrors :func:`collapsarr.jobs.routes.
    get_job_scheduler`'s fail-loudly convention rather than silently no-oping.
    """
    scheduler: HealthCheckScheduler | None = getattr(request.app.state, "health_scheduler", None)
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="Health check scheduler is not available.",
        )
    return scheduler


# --- schemas -----------------------------------------------------------------


class HealthCheckStateRead(BaseModel):
    """Response shape for one persisted health-check state row.

    Mirrors :class:`~collapsarr.health.models.HealthCheckState` field-for-field
    (``model_config = ConfigDict(from_attributes=True)`` lets FastAPI build
    this straight from the ORM row, same convention as
    ``collapsarr.jobs.routes.JobHistoryRead``). ``status`` is ``"passing"`` or
    ``"failing"`` (:data:`~collapsarr.health.models.CHECK_STATUS_PASSING` /
    :data:`~collapsarr.health.models.CHECK_STATUS_FAILING`); ``severity`` is
    ``"warning"`` or ``"error"``. ``dismissed_at`` (COL-82) is ``None`` unless
    an operator has dismissed this Check Key while it was failing.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    category: str
    status: str
    severity: str
    message: str
    instance_id: int | None
    first_failed_at: datetime | None
    last_checked_at: datetime
    dismissed_at: datetime | None


# --- endpoints ---------------------------------------------------------------


@router.get("/health-checks", response_model=list[HealthCheckStateRead])
def list_health_checks_endpoint(
    session: Session = Depends(get_session),
) -> list[HealthCheckState]:
    """List every registered check's current state, passing or failing.

    Ordered by ``code`` then ``instance_id`` (see
    :func:`~collapsarr.health.service.list_health_check_states`) -- generic
    over whatever checks are registered, with no per-check special-casing.
    Includes dismissed rows (``dismissed_at`` set) -- this page marks a
    dismissal, it never hides one; only the ``/health`` banner does that.
    """
    return list_health_check_states(session)


@router.post("/health-checks/recheck", response_model=list[HealthCheckStateRead])
def recheck_health_checks_endpoint(
    scheduler: HealthCheckScheduler = Depends(get_health_scheduler),
    session: Session = Depends(get_session),
) -> list[HealthCheckState]:
    """Run every registered check immediately and return the resulting state (COL-83).

    Lets an operator confirm a fix right away -- e.g. right after freeing disk
    space or fixing an Arr instance's URL -- rather than waiting up to
    :data:`~collapsarr.health.scheduler.INTERVAL_SECONDS` for the next
    scheduled tick.

    Calls :meth:`~collapsarr.health.scheduler.HealthCheckScheduler.run_once`
    directly -- the exact seam the periodic background loop's own iteration
    calls -- so this is a genuine extra, out-of-band tick, not a scheduler
    restart: it touches neither the loop's sleep/wake ``threading.Event`` nor
    its ``_thread``, so the next periodic tick still fires
    ``INTERVAL_SECONDS`` after whichever tick preceded *this* call, entirely
    unaffected by it. ``run_once`` persists through the same
    :func:`~collapsarr.health.service.reconcile_health_results` every tick
    uses, so a check whose result actually changed fires the identical
    edge-triggered pass<->fail notification a scheduled tick would -- there is
    no separate notification path for a manual recheck.

    Returns every persisted check's fresh, full-detail state (same shape as
    ``GET /health-checks`` above) so the caller can render the outcome without
    a second round-trip.
    """
    scheduler.run_once()
    return list_health_check_states(session)


@router.post("/health-checks/{check_id}/dismiss", response_model=HealthCheckStateRead)
def dismiss_health_check_endpoint(
    check_id: int,
    session: Session = Depends(get_session),
) -> HealthCheckState:
    """Dismiss one currently-failing check-state row (COL-82).

    Hides it from the ``/health`` banner while it stays visible -- marked
    dismissed -- on this list endpoint's response, until it is undismissed or
    next transitions from passing back to failing (auto-clearing the
    dismissal; see :func:`~collapsarr.health.service.reconcile_health_results`).

    ``404`` for an unknown ``check_id``; ``409`` if the row is not currently
    failing -- only a currently-failing Check Key makes sense to dismiss.
    """
    try:
        return dismiss_health_check(session, check_id)
    except HealthCheckStateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HealthCheckNotFailingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/health-checks/{check_id}/undismiss", response_model=HealthCheckStateRead)
def undismiss_health_check_endpoint(
    check_id: int,
    session: Session = Depends(get_session),
) -> HealthCheckState:
    """Clear a dismissal on one check-state row early (COL-82).

    ``404`` for an unknown ``check_id``; otherwise idempotent -- succeeds
    whether or not the row was actually dismissed, and regardless of its
    current passing/failing status.
    """
    try:
        return undismiss_health_check(session, check_id)
    except HealthCheckStateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
