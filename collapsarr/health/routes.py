"""HTTP REST endpoint for the full per-check health state (COL-76).

Thin layer over :func:`collapsarr.health.service.list_health_check_states`,
exposed as a FastAPI :class:`~fastapi.APIRouter` mounted under ``/api/system``
by :func:`collapsarr.main.create_app`. Because everything under ``/api`` is
gated by the auth middleware (session cookie or API key; see
:mod:`collapsarr.auth.enforcement`), the route here inherits that gate -- no
per-route auth wiring is needed. This is distinct from the unauthenticated
``/health`` liveness probe (:mod:`collapsarr.main`), which only ever surfaces
*currently-failing* checks with a minimal ``{code, message, severity}`` shape
for the app-wide banner; this endpoint returns every registered check's full,
current-tick detail (passing or failing) for the System > Health page.

One endpoint:

* ``GET /api/system/health-checks`` -- lists every persisted
  :class:`~collapsarr.health.models.HealthCheckState` row (one per *Check Key*
  -- ``(code, instance_id)``, see ``CONTEXT.md``), ordered by code then
  instance. Generic over whatever checks are registered
  (:func:`~collapsarr.health.registry.default_health_checks`) -- nothing here
  is hardcoded to FFmpeg.

The response includes each row's ``id`` (its stable database primary key) so a
later slice can address one check state directly -- e.g. COL-82's
dismiss/undismiss and COL-83's manual recheck, both out of scope here.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_session
from .models import HealthCheckState
from .service import list_health_check_states

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
    ``"warning"`` or ``"error"``.
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


# --- endpoints ---------------------------------------------------------------


@router.get("/health-checks", response_model=list[HealthCheckStateRead])
def list_health_checks_endpoint(
    session: Session = Depends(get_session),
) -> list[HealthCheckState]:
    """List every registered check's current state, passing or failing.

    Ordered by ``code`` then ``instance_id`` (see
    :func:`~collapsarr.health.service.list_health_check_states`) -- generic
    over whatever checks are registered, with no per-check special-casing.
    """
    return list_health_check_states(session)
