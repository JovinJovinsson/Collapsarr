"""HTTP REST endpoints for the full per-check health state (COL-76, COL-82).

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

Both actions are scoped to one persisted row's ``id`` (its stable database
primary key, returned by the list endpoint precisely so a later slice could
address one check state directly) -- which is already the Check Key
disambiguator for a per-instance check, so no separate instance parameter is
needed.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_session
from .models import HealthCheckState
from .service import (
    HealthCheckNotFailingError,
    HealthCheckStateNotFoundError,
    dismiss_health_check,
    list_health_check_states,
    undismiss_health_check,
)

router = APIRouter(prefix="/api/system", tags=["system"])


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
