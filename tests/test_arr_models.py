"""Tests for the ArrInstance ORM model.

Exercises persistence directly against the ``session`` fixture (schema
created via :func:`collapsarr.database.init_db`), independent of the service
layer or the HTTP app.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from collapsarr.arr.models import ArrInstance, ConnectivityStatus, InstanceType


def test_create_and_read_instance(session: Session) -> None:
    """A newly created instance round-trips through the session."""
    instance = ArrInstance(
        name="Main Sonarr",
        type=InstanceType.SONARR,
        base_url="http://sonarr.local:8989",
        api_key="sonarr-api-key",
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)

    assert instance.id is not None
    assert instance.name == "Main Sonarr"
    assert instance.type == InstanceType.SONARR
    assert instance.status == ConnectivityStatus.UNKNOWN
    assert instance.status_error is None
    assert instance.version is None
    assert instance.created_at is not None
    assert instance.updated_at is not None


def test_sonarr_and_radarr_instances_coexist_independently(session: Session) -> None:
    """Multiple instances of both types can be stored side by side."""
    sonarr_a = ArrInstance(
        name="Sonarr A", type=InstanceType.SONARR, base_url="http://a:8989", api_key="a"
    )
    sonarr_b = ArrInstance(
        name="Sonarr B", type=InstanceType.SONARR, base_url="http://b:8989", api_key="b"
    )
    radarr_a = ArrInstance(
        name="Radarr A", type=InstanceType.RADARR, base_url="http://c:7878", api_key="c"
    )
    session.add_all([sonarr_a, sonarr_b, radarr_a])
    session.commit()

    rows = session.query(ArrInstance).order_by(ArrInstance.name).all()
    assert [row.name for row in rows] == ["Radarr A", "Sonarr A", "Sonarr B"]
    assert [row.type for row in rows] == [
        InstanceType.RADARR,
        InstanceType.SONARR,
        InstanceType.SONARR,
    ]


def test_duplicate_name_is_rejected(session: Session) -> None:
    """Instance names must be unique so configs stay distinguishable."""
    session.add(
        ArrInstance(name="Dup", type=InstanceType.SONARR, base_url="http://a:8989", api_key="a")
    )
    session.commit()

    session.add(
        ArrInstance(name="Dup", type=InstanceType.RADARR, base_url="http://b:7878", api_key="b")
    )
    with pytest.raises(IntegrityError):
        session.commit()
