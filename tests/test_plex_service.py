"""Tests for the Plex connection service layer (get-or-create + connectivity on save).

Connectivity is stubbed via ``httpx.MockTransport`` fed from the same
recorded fixtures used in ``test_plex_client.py`` — no live network call is
made, matching ``test_arr_service.py``'s treatment of the arr instance
service.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from collapsarr.plex.models import ConnectivityStatus, PlexConnection
from collapsarr.plex.service import get_plex_connection, update_plex_connection

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "plex"


def _ok_transport(version: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"MediaContainer": {"version": version}})

    return httpx.MockTransport(handler)


def _unauthorized_transport() -> httpx.MockTransport:
    payload = json.loads((FIXTURES_DIR / "unauthorized.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=payload)

    return httpx.MockTransport(handler)


def _configure(session: Session, *, base_url: str = "http://plex.local:32400") -> PlexConnection:
    """Save a working connection (base URL + token, connectivity OK) for a test to build on."""
    return update_plex_connection(
        session, base_url=base_url, token="plex-token", transport=_ok_transport("1.0.0")
    )


# ---------------------------------------------------------------------------
# get_plex_connection: get-or-create.
# ---------------------------------------------------------------------------


def test_get_plex_connection_creates_the_row_on_first_call(session: Session) -> None:
    assert session.scalars(select(PlexConnection)).one_or_none() is None

    connection = get_plex_connection(session)

    assert connection.id == 1
    assert session.scalars(select(PlexConnection)).one().id == connection.id


def test_get_plex_connection_defaults_are_blank_and_unchecked(session: Session) -> None:
    connection = get_plex_connection(session)

    assert connection.base_url == ""
    assert connection.token == ""
    assert connection.status == ConnectivityStatus.UNKNOWN
    assert connection.status_error is None
    assert connection.status_checked_at is None
    assert connection.version is None


def test_get_plex_connection_does_not_run_a_connectivity_check(session: Session) -> None:
    """Merely fetching the singleton (get-or-create) never dials out."""
    connection = get_plex_connection(session)

    assert connection.status == ConnectivityStatus.UNKNOWN
    assert connection.status_checked_at is None


def test_get_plex_connection_does_not_duplicate_the_row_across_calls(session: Session) -> None:
    first = get_plex_connection(session)
    second = get_plex_connection(session)

    assert first.id == second.id
    assert session.scalars(select(PlexConnection)).all() == [first]


# ---------------------------------------------------------------------------
# update_plex_connection: save + re-validate connectivity.
# ---------------------------------------------------------------------------


def test_update_plex_connection_persists_and_checks_connectivity(session: Session) -> None:
    transport = _ok_transport("1.32.5.7349-8f4248874")

    connection = update_plex_connection(
        session,
        base_url="http://plex.local:32400",
        token="plex-token",
        transport=transport,
    )

    assert connection.base_url == "http://plex.local:32400"
    assert connection.token == "plex-token"
    assert connection.status == ConnectivityStatus.OK
    assert connection.version == "1.32.5.7349-8f4248874"
    assert connection.status_error is None
    assert connection.status_checked_at is not None


def test_update_plex_connection_records_connectivity_failure(session: Session) -> None:
    """A failed connectivity check still saves the row, with the failure recorded."""
    transport = _unauthorized_transport()

    connection = update_plex_connection(
        session,
        base_url="http://plex.local:32400",
        token="wrong-token",
        transport=transport,
    )

    assert connection.base_url == "http://plex.local:32400"
    assert connection.status == ConnectivityStatus.ERROR
    assert connection.status_error is not None
    assert connection.version is None


def test_update_plex_connection_creates_the_row_if_absent(session: Session) -> None:
    assert session.scalars(select(PlexConnection)).one_or_none() is None

    connection = _configure(session)

    assert connection.id == 1


def test_update_plex_connection_changes_only_the_given_fields(session: Session) -> None:
    _configure(session)

    updated = update_plex_connection(
        session, base_url="http://plex.local:32401", transport=_ok_transport("1.0.0")
    )

    assert updated.base_url == "http://plex.local:32401"
    # Token is untouched since it wasn't passed.
    assert updated.token == "plex-token"


def test_update_plex_connection_strips_trailing_slash_from_base_url(session: Session) -> None:
    connection = update_plex_connection(
        session,
        base_url="http://plex.local:32400/",
        token="plex-token",
        transport=_ok_transport("1.0.0"),
    )

    assert connection.base_url == "http://plex.local:32400"


def test_update_plex_connection_re_validates_on_every_save(session: Session) -> None:
    """Saving always re-runs the connectivity check, even with no field changes."""
    _configure(session)

    re_saved = update_plex_connection(session, transport=_unauthorized_transport())

    assert re_saved.status == ConnectivityStatus.ERROR
    assert re_saved.base_url == "http://plex.local:32400"


def test_update_plex_connection_persists_across_a_fresh_read(session: Session) -> None:
    _configure(session)

    reread = get_plex_connection(session)

    assert reread.base_url == "http://plex.local:32400"
    assert reread.status == ConnectivityStatus.OK


# ---------------------------------------------------------------------------
# Public re-exports.
# ---------------------------------------------------------------------------


def test_plex_connection_importable_from_package_root() -> None:
    """Sanity check the public re-exports from collapsarr.plex."""
    from collapsarr.plex import PlexConnection as ReexportedPlexConnection
    from collapsarr.plex import get_plex_connection as reexported_get
    from collapsarr.plex import update_plex_connection as reexported_update

    assert ReexportedPlexConnection is PlexConnection
    assert reexported_get is get_plex_connection
    assert reexported_update is update_plex_connection
