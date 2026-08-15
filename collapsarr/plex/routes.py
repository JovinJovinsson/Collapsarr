"""HTTP REST endpoints for the singleton Plex connection config (COL-209).

Thin GET/PUT layer over :mod:`collapsarr.plex.service`, exposed as a FastAPI
:class:`~fastapi.APIRouter` mounted under ``/api`` by
:func:`collapsarr.main.create_app`, matching :mod:`collapsarr.notify.routes`'s
pattern for its own singleton row. Because everything under ``/api`` is gated
by the API-key/session auth middleware (COL-26/COL-50), both routes here
inherit that auth -- no per-route auth wiring is needed.

Security-critical: the Plex token must never reach the browser (COL-209's
acceptance criteria). :class:`PlexConnectionRead` -- the shape returned by
both ``GET`` and ``PUT`` -- has no ``token`` field at all; only a derived
``has_token`` boolean is exposed, so the UI can tell "a token is configured"
from "no token yet" without ever being able to read the secret itself. This
is a deliberate departure from :mod:`collapsarr.arr.routes`'s ``InstanceRead``
(which does echo back ``api_key``) -- Plex's token is treated as a strictly
higher-sensitivity secret than an Arr API key.

``PUT /api/plex/connection`` follows the same partial-update convention as
:func:`collapsarr.notify.routes.update_notifiers_endpoint`: only fields
present in the request body are changed. Omitting ``token`` leaves the
currently-stored token untouched -- since a ``GET`` never echoes it back,
the frontend has no way to "round-trip" it, so the edit form simply leaves
the token field blank and only sends it when the operator types a new one.
Saving also **kicks off a background Plex Sync** (COL-210) via the scheduler's
non-blocking :meth:`~collapsarr.plex.scheduler.PlexSyncScheduler.request_sync`,
so a freshly-saved/reconnected server's library is mapped without waiting for
the weekly cadence -- and without blocking this save's HTTP response.

``POST /api/plex/sync`` (COL-210) is the manual "Run now" trigger for the Plex
Sync Scheduled Task (surfaced on ``/system/tasks``): it rebuilds the Plex
Library Item mapping table synchronously and returns a ``202`` with the row
count, mirroring ``POST /api/jobs/scan``'s "Scan now" shape.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_session
from .models import ConnectivityStatus, PlexConnection
from .scheduler import PlexSyncScheduler
from .service import get_plex_connection, update_plex_connection

router = APIRouter(prefix="/api", tags=["plex"])


def _plex_sync_scheduler(request: Request) -> PlexSyncScheduler:
    """Return the live Plex Sync scheduler, or ``503`` if the app was built without it.

    ``create_app`` wires ``plex_sync_scheduler`` onto ``app.state``
    unconditionally, so in a normally-built app this always resolves; the guard
    only trips for a deliberately-stripped-down app (mirroring
    :func:`collapsarr.jobs.routes.get_job_scheduler`'s own 503 for the job
    scheduler).
    """
    scheduler: PlexSyncScheduler | None = getattr(request.app.state, "plex_sync_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Plex Sync scheduler is not enabled")
    return scheduler


# --- schemas -----------------------------------------------------------------


class PlexConnectionRead(BaseModel):
    """Response shape for the Plex connection row. Never includes the token."""

    base_url: str
    has_token: bool
    status: ConnectivityStatus
    status_error: str | None
    status_checked_at: datetime | None
    version: str | None
    created_at: datetime
    updated_at: datetime


class PlexSyncRunResult(BaseModel):
    """Response for ``POST /api/plex/sync``: the mapping table's row count after the rebuild."""

    items: int


class PlexConnectionUpdate(BaseModel):
    """Request body to update the Plex connection; omitted fields are left unchanged.

    ``token`` is write-only: it is accepted here but never returned by
    :class:`PlexConnectionRead`. Omitting it leaves the stored token
    untouched (it does *not* clear it) -- there is no "clear the token"
    affordance, matching how Sonarr/Radarr's ``api_key`` is always required.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    token: str | None = None


def _to_read(connection: PlexConnection) -> PlexConnectionRead:
    """Adapt a persisted :class:`PlexConnection` row into its JSON response shape.

    ``has_token`` is derived from the stored token's presence -- the token
    value itself never appears in the returned object.
    """
    return PlexConnectionRead(
        base_url=connection.base_url,
        has_token=bool(connection.token),
        status=connection.status,
        status_error=connection.status_error,
        status_checked_at=connection.status_checked_at,
        version=connection.version,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


# --- endpoints ---------------------------------------------------------------


@router.get("/plex/connection", response_model=PlexConnectionRead)
def get_plex_connection_endpoint(session: Session = Depends(get_session)) -> PlexConnectionRead:
    """Return the Plex connection row, creating it with blank defaults on first read."""
    return _to_read(get_plex_connection(session))


@router.put("/plex/connection", response_model=PlexConnectionRead)
def update_plex_connection_endpoint(
    body: PlexConnectionUpdate, request: Request, session: Session = Depends(get_session)
) -> PlexConnectionRead:
    """Update the provided fields, re-validate connectivity, and kick off a Plex Sync.

    A ``None`` field (whether omitted or explicitly sent as ``null``) is left
    unchanged by :func:`~collapsarr.plex.service.update_plex_connection` --
    there is no "clear to empty" affordance for ``base_url``/``token``
    (matching :class:`PlexConnectionUpdate`'s own docstring), so fields can be
    passed straight through by name, the same direct form
    :func:`collapsarr.arr.routes.update_instance_endpoint` uses. Saving always
    re-runs the connectivity check and persists the outcome, the same way
    ``PUT /api/instances/{id}`` does for Sonarr/Radarr.

    On save/reconnect it also asks the Plex Sync scheduler to take an off-cycle
    sync (COL-210), via the non-blocking
    :meth:`~collapsarr.plex.scheduler.PlexSyncScheduler.request_sync` -- so a
    just-configured server's library is mapped promptly rather than at the next
    weekly tick, without this response waiting on the walk. The trigger fires
    regardless of the connectivity outcome: an operator correcting a URL/token
    wants the retry to remap, and a still-broken connection simply makes the
    background sync a no-op.
    """
    connection = _to_read(
        update_plex_connection(session, base_url=body.base_url, token=body.token)
    )
    _plex_sync_scheduler(request).request_sync()
    return connection


@router.post("/plex/sync", status_code=202, response_model=PlexSyncRunResult)
def run_plex_sync_endpoint(request: Request) -> PlexSyncRunResult:
    """Rebuild the Plex Library Item mapping table now -- the "Run now" trigger (COL-210).

    Drives :meth:`~collapsarr.plex.scheduler.PlexSyncScheduler.run_once`
    synchronously (the same method the weekly loop and the on-save trigger run)
    and returns ``202`` with the rebuilt table's row count. The Scheduled Task
    row on ``/system/tasks`` reads its refreshed ``last_run_at`` afterward, the
    same "Run now then refetch" shape ``POST /api/jobs/scan`` uses for the
    library scan.
    """
    return PlexSyncRunResult(items=_plex_sync_scheduler(request).run_once())
