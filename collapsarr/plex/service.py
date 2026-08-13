"""Service-layer read/write interface for the singleton Plex connection (COL-209).

Plain functions taking a SQLAlchemy :class:`~sqlalchemy.orm.Session`, matching
the pattern :mod:`collapsarr.notify.service` and :mod:`collapsarr.settings.
service` already use for their own singleton rows. HTTP exposure is
:mod:`collapsarr.plex.routes`'s concern -- this module is the whole storage +
connectivity surface.

:func:`get_plex_connection` is get-or-create: it returns the singleton row,
creating it with blank defaults (empty ``base_url``/``token``, ``status``
``unknown``) on first call if it doesn't exist yet -- mirroring
:func:`collapsarr.notify.service.get_notifier_config`. It does **not** run a
connectivity check; that only happens on an explicit save
(:func:`update_plex_connection`), matching the acceptance criterion "saving
re-validates connectivity".

:func:`update_plex_connection` changes only the fields passed explicitly, then
always re-validates connectivity and stamps the outcome -- the same
"persist regardless of outcome, but always check" contract
:func:`collapsarr.arr.service.update_instance` gives
:class:`~collapsarr.arr.models.ArrInstance`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from .client import check_connectivity
from .models import PLEX_CONNECTION_ID, ConnectivityStatus, PlexConnection


def get_plex_connection(session: Session) -> PlexConnection:
    """Return the singleton Plex connection row, creating it with defaults if absent.

    Safe to call repeatedly -- once created, the same row is always returned;
    it is never recreated or duplicated (enforced at the schema level by
    :class:`~collapsarr.plex.models.PlexConnection`'s singleton check
    constraint).
    """
    connection = session.get(PlexConnection, PLEX_CONNECTION_ID)
    if connection is None:
        connection = PlexConnection(id=PLEX_CONNECTION_ID)
        session.add(connection)
        session.commit()
        session.refresh(connection)
    return connection


def _apply_connectivity_check(
    connection: PlexConnection, *, transport: httpx.BaseTransport | None
) -> None:
    """Run the connectivity/version check and stamp its outcome onto ``connection``."""
    result = check_connectivity(connection.base_url, connection.token, transport=transport)
    connection.status = ConnectivityStatus.OK if result.ok else ConnectivityStatus.ERROR
    connection.status_error = result.error
    connection.version = result.version
    connection.status_checked_at = datetime.now(UTC)


def update_plex_connection(
    session: Session,
    *,
    base_url: str | None = None,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> PlexConnection:
    """Update the given fields on the Plex connection row and re-validate connectivity.

    Only fields passed explicitly (non-``None``) are changed, matching
    :func:`collapsarr.arr.service.update_instance`. The row is persisted
    regardless of whether the connectivity check succeeds -- ``status``/
    ``status_error`` record the outcome so a misconfigured connection remains
    visible (and editable) rather than being silently dropped, same as
    :class:`~collapsarr.arr.models.ArrInstance`. Creates the row with defaults
    first if it doesn't exist yet, same as :func:`get_plex_connection`.
    """
    connection = get_plex_connection(session)

    if base_url is not None:
        connection.base_url = base_url.rstrip("/")
    if token is not None:
        connection.token = token

    _apply_connectivity_check(connection, transport=transport)
    session.commit()
    session.refresh(connection)
    return connection
