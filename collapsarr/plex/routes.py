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
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_session
from .models import ConnectivityStatus, PlexConnection
from .service import get_plex_connection, update_plex_connection

router = APIRouter(prefix="/api", tags=["plex"])


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
    body: PlexConnectionUpdate, session: Session = Depends(get_session)
) -> PlexConnectionRead:
    """Update the provided fields and re-validate connectivity.

    A ``None`` field (whether omitted or explicitly sent as ``null``) is left
    unchanged by :func:`~collapsarr.plex.service.update_plex_connection` --
    there is no "clear to empty" affordance for ``base_url``/``token``
    (matching :class:`PlexConnectionUpdate`'s own docstring), so fields can be
    passed straight through by name, the same direct form
    :func:`collapsarr.arr.routes.update_instance_endpoint` uses. Saving always
    re-runs the connectivity check and persists the outcome, the same way
    ``PUT /api/instances/{id}`` does for Sonarr/Radarr.
    """
    return _to_read(update_plex_connection(session, base_url=body.base_url, token=body.token))
