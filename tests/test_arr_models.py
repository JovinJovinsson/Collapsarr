"""Tests for the ArrInstance ORM model.

Exercises persistence directly against the ``session`` fixture (schema
created via :func:`collapsarr.database.init_db`), independent of the service
layer or the HTTP app.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from collapsarr.arr.models import (
    ArrInstance,
    ConnectivityStatus,
    InstanceType,
    RemotePathMapping,
    resolve_path,
)


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


def test_remote_path_mapping_creates_and_rounds_trips(session: Session) -> None:
    """A newly created remote path mapping persists and round-trips correctly."""
    instance = ArrInstance(
        name="Sonarr with mappings",
        type=InstanceType.SONARR,
        base_url="http://sonarr.local:8989",
        api_key="key",
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)

    mapping = RemotePathMapping(
        instance_id=instance.id,
        remote_prefix="/tv",
        local_prefix="/mnt/media/tv",
        order=0,
    )
    session.add(mapping)
    session.commit()
    session.refresh(mapping)

    assert mapping.id is not None
    assert mapping.instance_id == instance.id
    assert mapping.remote_prefix == "/tv"
    assert mapping.local_prefix == "/mnt/media/tv"
    assert mapping.order == 0
    assert mapping.created_at is not None
    assert mapping.updated_at is not None


def test_multiple_mappings_per_instance(session: Session) -> None:
    """Multiple path mappings can be configured for a single instance."""
    instance = ArrInstance(
        name="Multi-mapped instance",
        type=InstanceType.RADARR,
        base_url="http://radarr.local:7878",
        api_key="key",
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)

    mapping1 = RemotePathMapping(
        instance_id=instance.id,
        remote_prefix="/tv",
        local_prefix="/mnt/media/tv",
        order=0,
    )
    mapping2 = RemotePathMapping(
        instance_id=instance.id,
        remote_prefix="/movies",
        local_prefix="/mnt/media/movies",
        order=1,
    )
    session.add_all([mapping1, mapping2])
    session.commit()

    # Verify both mappings exist and belong to the same instance
    mappings = session.query(RemotePathMapping).filter_by(instance_id=instance.id).all()
    assert len(mappings) == 2
    assert {m.remote_prefix for m in mappings} == {"/tv", "/movies"}


def test_resolve_path_returns_unchanged_when_no_mappings() -> None:
    """resolve_path returns the original path when no mappings are provided."""
    path = "/container/path/to/file.mkv"
    assert resolve_path(path) == path
    assert resolve_path(path, None) == path
    assert resolve_path(path, []) == path


def test_resolve_path_applies_matching_mapping() -> None:
    """resolve_path transforms a path using a matching remote prefix."""
    mapping = RemotePathMapping(
        id=1,
        instance_id=1,
        remote_prefix="/tv",
        local_prefix="/mnt/media/tv",
        order=0,
    )
    path = "/tv/Show/Season 01/Episode.mkv"
    expected = "/mnt/media/tv/Show/Season 01/Episode.mkv"
    assert resolve_path(path, [mapping]) == expected


def test_resolve_path_passes_through_unmapped_paths() -> None:
    """resolve_path returns the original path when no mapping matches."""
    mapping = RemotePathMapping(
        id=1,
        instance_id=1,
        remote_prefix="/tv",
        local_prefix="/mnt/media/tv",
        order=0,
    )
    path = "/movies/Film/file.mkv"
    assert resolve_path(path, [mapping]) == path


def test_resolve_path_uses_first_matching_mapping() -> None:
    """resolve_path applies the first matching mapping when multiple could apply."""
    mapping1 = RemotePathMapping(
        id=1,
        instance_id=1,
        remote_prefix="/tv",
        local_prefix="/mnt/media/tv",
        order=0,
    )
    mapping2 = RemotePathMapping(
        id=2,
        instance_id=1,
        remote_prefix="/tv/Shows",
        local_prefix="/mnt/shows",
        order=1,
    )
    path = "/tv/Shows/MyShow/Season 01/Episode.mkv"
    # mapping1 matches and is first, so it should be applied
    expected = "/mnt/media/tv/Shows/MyShow/Season 01/Episode.mkv"
    assert resolve_path(path, [mapping1, mapping2]) == expected


def test_resolve_path_handles_multiple_distinct_prefixes() -> None:
    """resolve_path applies the correct mapping for each distinct prefix."""
    tv_mapping = RemotePathMapping(
        id=1,
        instance_id=1,
        remote_prefix="/tv",
        local_prefix="/mnt/media/tv",
        order=0,
    )
    movies_mapping = RemotePathMapping(
        id=2,
        instance_id=1,
        remote_prefix="/movies",
        local_prefix="/mnt/media/movies",
        order=1,
    )
    mappings = [tv_mapping, movies_mapping]

    tv_path = "/tv/Show/Season 01/file.mkv"
    assert resolve_path(tv_path, mappings) == "/mnt/media/tv/Show/Season 01/file.mkv"

    movies_path = "/movies/Film/file.mkv"
    assert resolve_path(movies_path, mappings) == "/mnt/media/movies/Film/file.mkv"

    unmapped_path = "/other/path/file.mkv"
    assert resolve_path(unmapped_path, mappings) == unmapped_path


def test_resolve_path_with_path_normalization(session: Session) -> None:
    """resolve_path correctly handles paths with directory separators."""
    mapping = RemotePathMapping(
        id=1,
        instance_id=1,
        remote_prefix="/tv",
        local_prefix="/mnt/media/tv",
        order=0,
    )
    # Test with double slashes and trailing components
    path = "/tv/Show/Season 01/Episode.mkv"
    expected = "/mnt/media/tv/Show/Season 01/Episode.mkv"
    assert resolve_path(path, [mapping]) == expected

    # Test edge case: prefix exactly matches the entire path
    exact_path = "/tv"
    expected_exact = "/mnt/media/tv"
    assert resolve_path(exact_path, [mapping]) == expected_exact
