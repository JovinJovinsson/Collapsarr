"""Contract tests for the wanted-list REST endpoint (COL-28).

Covers request/response shape and the API-key-required behaviour (COL-26) for
``GET /api/wanted``. Tracked media is seeded through the real
:mod:`collapsarr.media.service` upsert path (via the shared ``session``
fixture, which shares the SQLite file the ``client`` app reads), so the
endpoint exercises the genuine "files missing at least one enabled target"
query rather than a stub.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.arr.catalog import (
    CatalogEpisode,
    CatalogMovie,
    CatalogSeries,
    RadarrCatalog,
    SonarrCatalog,
)
from collapsarr.arr.models import ArrInstance, InstanceType
from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.library.service import set_tracked, sync_library
from collapsarr.media.service import upsert_tracked_media
from collapsarr.settings.service import get_global_settings, update_global_settings

ALL_TARGETS = frozenset(
    {DownmixTarget.STEREO, DownmixTarget.TWO_POINT_ONE, DownmixTarget.FIVE_POINT_ONE}
)


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _stream(*, channels: int, language: str = "eng") -> AudioStreamInfo:
    return AudioStreamInfo(
        index=0,
        codec="flac",
        channels=channels,
        channel_layout=f"{channels}ch",
        language=language,
    )


# --- shape --------------------------------------------------------------------


def test_wanted_is_empty_when_nothing_tracked(client: TestClient) -> None:
    response = client.get("/api/wanted", headers=_auth_headers(client))
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_wanted_lists_files_missing_enabled_targets(
    client: TestClient, session: Session
) -> None:
    # Enable every target, then track an 8-channel file: all three targets
    # qualify and are recorded MISSING.
    update_global_settings(session, enabled_targets=ALL_TARGETS)
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    response = client.get("/api/wanted", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["file_path"] == "/media/movie.mkv"
    assert row["id"] is not None
    assert "created_at" in row and "updated_at" in row
    missing = {(m["language"], m["target"]) for m in row["missing_targets"]}
    assert missing == {("eng", "stereo"), ("eng", "2.1"), ("eng", "5.1")}


def test_wanted_reflects_currently_enabled_targets(
    client: TestClient, session: Session
) -> None:
    """A target no longer enabled drops out of the wanted-list immediately."""
    # Record MISSING rows for all three targets...
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )
    # ...but only enable Stereo in the live settings.
    update_global_settings(session, enabled_targets=frozenset({DownmixTarget.STEREO}))

    response = client.get("/api/wanted", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    missing = {(m["language"], m["target"]) for m in rows[0]["missing_targets"]}
    assert missing == {("eng", "stereo")}


def test_wanted_excludes_fully_processed_files(
    client: TestClient, session: Session
) -> None:
    """A file whose only stream already sits at every enabled target is not wanted."""
    update_global_settings(session, enabled_targets=frozenset({DownmixTarget.STEREO}))
    # A 2-channel eng stream already satisfies Stereo -> nothing missing.
    upsert_tracked_media(
        session,
        file_path="/media/already-stereo.mkv",
        streams=[_stream(channels=2)],
        settings=DownmixSettings(enabled_targets=frozenset({DownmixTarget.STEREO})),
    )

    response = client.get("/api/wanted", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    assert response.json() == []


# --- Library node Tracked bridge (COL-101) -------------------------------------


def _seed_sonarr_instance_and_episode(session: Session) -> tuple[ArrInstance, int]:
    """Seed a Sonarr instance with one synced episode; return (instance, sonarr_episode_id)."""
    instance = ArrInstance(
        name="Sonarr", type=InstanceType.SONARR, base_url="http://sonarr.local", api_key="k"
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)
    catalog = SonarrCatalog(
        instance_id=instance.id,
        series=(
            CatalogSeries(
                series_id=1,
                title="Breaking Bad",
                season_numbers=(1,),
                episodes=(CatalogEpisode(101, 1, 1, "Pilot", has_file=True),),
            ),
        ),
    )
    sync_library(session, instance_id=instance.id, catalog=catalog)
    return instance, 101


def test_wanted_includes_the_resolved_tracked_bridge_when_it_resolves(
    client: TestClient, session: Session
) -> None:
    instance, episode_id = _seed_sonarr_instance_and_episode(session)
    update_global_settings(session, enabled_targets=ALL_TARGETS)
    upsert_tracked_media(
        session,
        file_path="/media/pilot.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
        instance_id=instance.id,
        sonarr_episode_id=episode_id,
    )

    response = client.get("/api/wanted", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    row = response.json()[0]
    assert row["node_type"] == "episode"
    assert row["tracked"] is True  # global default_tracked
    assert isinstance(row["library_node_id"], int)


def test_wanted_excludes_a_file_marked_not_tracked(
    client: TestClient, session: Session
) -> None:
    """AC2: a file whose node resolves Not-Tracked drops out of /api/wanted (COL-102)."""
    instance, episode_id = _seed_sonarr_instance_and_episode(session)
    update_global_settings(session, enabled_targets=ALL_TARGETS)
    upsert_tracked_media(
        session,
        file_path="/media/pilot.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
        instance_id=instance.id,
        sonarr_episode_id=episode_id,
    )
    # While Tracked (the global default), it is wanted...
    response = client.get("/api/wanted", headers=_auth_headers(client))
    assert [row["file_path"] for row in response.json()] == ["/media/pilot.mkv"]
    node_id = response.json()[0]["library_node_id"]

    # ...but flipping its node to Not-Tracked removes it entirely (even though
    # its MISSING target rows still exist).
    set_tracked(session, node_id=node_id, tracked=False)

    response = client.get("/api/wanted", headers=_auth_headers(client))
    assert response.json() == []


def test_wanted_tracked_fields_are_none_when_the_bridge_is_unresolved(
    client: TestClient, session: Session
) -> None:
    """A file with no captured instance/episode id (e.g. a manual trigger) degrades gracefully."""
    update_global_settings(session, enabled_targets=ALL_TARGETS)
    upsert_tracked_media(
        session,
        file_path="/media/untracked-origin.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    response = client.get("/api/wanted", headers=_auth_headers(client))

    row = response.json()[0]
    assert row["library_node_id"] is None
    assert row["node_type"] is None
    assert row["tracked"] is None


def test_wanted_tracked_bridge_for_a_radarr_movie(client: TestClient, session: Session) -> None:
    instance = ArrInstance(
        name="Radarr", type=InstanceType.RADARR, base_url="http://radarr.local", api_key="k"
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)
    catalog = RadarrCatalog(
        instance_id=instance.id, movies=(CatalogMovie(movie_id=1, title="Arrival", has_file=True),)
    )
    sync_library(session, instance_id=instance.id, catalog=catalog)
    update_global_settings(session, enabled_targets=ALL_TARGETS)
    upsert_tracked_media(
        session,
        file_path="/media/arrival.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
        instance_id=instance.id,
        radarr_movie_id=1,
    )

    response = client.get("/api/wanted", headers=_auth_headers(client))

    row = response.json()[0]
    assert row["node_type"] == "movie"
    assert row["tracked"] is True


# --- auth-required behaviour ---------------------------------------------------


def test_wanted_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)

    response = client.get("/api/wanted")
    assert response.status_code == 401
