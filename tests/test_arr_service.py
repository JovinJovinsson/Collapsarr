"""Tests for the arr instance service layer (CRUD + connectivity on save).

Connectivity is stubbed via ``httpx.MockTransport`` fed from the same
recorded fixtures used in ``test_arr_client.py`` — no live network call is
made.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from collapsarr.arr.models import ConnectivityStatus, InstanceType
from collapsarr.arr.service import (
    InstanceNotFoundError,
    create_instance,
    delete_instance,
    get_instance,
    list_instances,
    update_instance,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arr"


def _ok_transport(version: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": version})

    return httpx.MockTransport(handler)


def _unauthorized_transport() -> httpx.MockTransport:
    payload = json.loads((FIXTURES_DIR / "unauthorized.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=payload)

    return httpx.MockTransport(handler)


def test_create_instance_persists_and_checks_connectivity(session: Session) -> None:
    """Creating an instance persists it and stores a successful connectivity check."""
    transport = _ok_transport("4.0.1.929")

    instance = create_instance(
        session,
        name="Main Sonarr",
        instance_type=InstanceType.SONARR,
        base_url="http://sonarr.local:8989",
        api_key="sonarr-api-key",
        transport=transport,
    )

    assert instance.id is not None
    assert instance.status == ConnectivityStatus.OK
    assert instance.version == "4.0.1.929"
    assert instance.status_error is None
    assert instance.status_checked_at is not None


def test_create_instance_records_connectivity_failure(session: Session) -> None:
    """A failed connectivity check still saves the row, with the failure recorded."""
    transport = _unauthorized_transport()

    instance = create_instance(
        session,
        name="Bad Sonarr",
        instance_type=InstanceType.SONARR,
        base_url="http://sonarr.local:8989",
        api_key="wrong-key",
        transport=transport,
    )

    assert instance.id is not None
    assert instance.status == ConnectivityStatus.ERROR
    assert instance.version is None
    assert instance.status_error is not None


def test_multiple_instances_of_both_types_coexist_independently(session: Session) -> None:
    """Several Sonarr and Radarr instances can be created and listed independently."""
    create_instance(
        session,
        name="Sonarr A",
        instance_type=InstanceType.SONARR,
        base_url="http://a:8989",
        api_key="a",
        transport=_ok_transport("4.0.1.929"),
    )
    create_instance(
        session,
        name="Sonarr B",
        instance_type=InstanceType.SONARR,
        base_url="http://b:8989",
        api_key="b",
        transport=_ok_transport("4.0.1.929"),
    )
    create_instance(
        session,
        name="Radarr A",
        instance_type=InstanceType.RADARR,
        base_url="http://c:7878",
        api_key="c",
        transport=_ok_transport("5.4.6.8723"),
    )

    instances = list_instances(session)

    assert len(instances) == 3
    sonarr_names = {i.name for i in instances if i.type == InstanceType.SONARR}
    radarr_names = {i.name for i in instances if i.type == InstanceType.RADARR}
    assert sonarr_names == {"Sonarr A", "Sonarr B"}
    assert radarr_names == {"Radarr A"}


def test_get_instance_returns_none_when_missing(session: Session) -> None:
    """get_instance returns None (not an exception) for an unknown id."""
    assert get_instance(session, 999) is None


def test_update_instance_changes_fields_and_rechecks_connectivity(session: Session) -> None:
    """Updating an instance re-validates connectivity and reflects a fixed API key."""
    instance = create_instance(
        session,
        name="Sonarr",
        instance_type=InstanceType.SONARR,
        base_url="http://sonarr.local:8989",
        api_key="wrong-key",
        transport=_unauthorized_transport(),
    )
    assert instance.status == ConnectivityStatus.ERROR

    updated = update_instance(
        session,
        instance.id,
        api_key="correct-key",
        transport=_ok_transport("4.0.1.929"),
    )

    assert updated.id == instance.id
    assert updated.api_key == "correct-key"
    assert updated.status == ConnectivityStatus.OK
    assert updated.version == "4.0.1.929"
    assert updated.status_error is None


def test_update_instance_raises_for_unknown_id(session: Session) -> None:
    """Updating a nonexistent instance raises InstanceNotFoundError."""
    with pytest.raises(InstanceNotFoundError):
        update_instance(session, 999, name="Doesn't matter")


def test_delete_instance_removes_it(session: Session) -> None:
    """Deleting an instance removes it from subsequent listings."""
    instance = create_instance(
        session,
        name="Sonarr",
        instance_type=InstanceType.SONARR,
        base_url="http://sonarr.local:8989",
        api_key="key",
        transport=_ok_transport("4.0.1.929"),
    )

    delete_instance(session, instance.id)

    assert get_instance(session, instance.id) is None
    assert list_instances(session) == []


def test_delete_instance_raises_for_unknown_id(session: Session) -> None:
    """Deleting a nonexistent instance raises InstanceNotFoundError."""
    with pytest.raises(InstanceNotFoundError):
        delete_instance(session, 999)


def test_deleting_one_instance_leaves_others_untouched(session: Session) -> None:
    """Deleting one instance doesn't affect independently configured instances."""
    keep = create_instance(
        session,
        name="Keep",
        instance_type=InstanceType.RADARR,
        base_url="http://keep:7878",
        api_key="keep-key",
        transport=_ok_transport("5.4.6.8723"),
    )
    remove = create_instance(
        session,
        name="Remove",
        instance_type=InstanceType.SONARR,
        base_url="http://remove:8989",
        api_key="remove-key",
        transport=_ok_transport("4.0.1.929"),
    )

    delete_instance(session, remove.id)

    remaining = list_instances(session)
    assert [i.id for i in remaining] == [keep.id]
