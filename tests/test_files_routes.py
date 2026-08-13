"""Contract tests for the single-file lookup REST endpoint (COL-203).

Covers request/response shape and the API-key-required behaviour (COL-26) for
``GET /api/files/{file_id}``. Unlike ``GET /api/wanted`` (COL-28), this
endpoint resolves a tracked file by id regardless of whether it currently
has any missing targets -- the key regression this ticket exists to fix
(previously the file detail page scanned ``GET /api/wanted``, which excludes
fully-processed files entirely). Tracked media is seeded through the real
:mod:`collapsarr.media.service` upsert path (via the shared ``session``
fixture, which shares the SQLite file the ``client`` app reads), so the
endpoint exercises genuine data rather than a stub.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.arr.catalog import CatalogEpisode, CatalogSeries, SonarrCatalog
from collapsarr.arr.models import ArrInstance, InstanceType
from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.library.service import sync_library
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


def test_files_endpoint_returns_a_file_with_missing_targets(
    client: TestClient, session: Session
) -> None:
    update_global_settings(session, enabled_targets=ALL_TARGETS)
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    response = client.get(f"/api/files/{media.id}", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == media.id
    assert body["file_path"] == "/media/movie.mkv"
    missing = {(m["language"], m["target"]) for m in body["missing_targets"]}
    assert missing == {("eng", "stereo"), ("eng", "2.1"), ("eng", "5.1")}


def test_files_endpoint_returns_a_fully_processed_file_excluded_from_wanted(
    client: TestClient, session: Session
) -> None:
    """The key regression this ticket fixes: a file with zero missing targets
    (so it's excluded from `GET /api/wanted`) must still resolve by id."""
    update_global_settings(session, enabled_targets=frozenset({DownmixTarget.STEREO}))
    media = upsert_tracked_media(
        session,
        file_path="/media/already-stereo.mkv",
        streams=[_stream(channels=2)],
        settings=DownmixSettings(enabled_targets=frozenset({DownmixTarget.STEREO})),
    )

    # Confirm it's absent from the wanted list first.
    wanted_response = client.get("/api/wanted", headers=_auth_headers(client))
    assert wanted_response.json() == []

    response = client.get(f"/api/files/{media.id}", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == media.id
    assert body["file_path"] == "/media/already-stereo.mkv"
    assert body["missing_targets"] == []


def test_files_endpoint_returns_not_found_for_an_id_that_never_existed(
    client: TestClient, session: Session
) -> None:
    response = client.get("/api/files/999999", headers=_auth_headers(client))

    assert response.status_code == 404, response.text


def test_files_endpoint_bridges_tracked_status_the_same_as_wanted(
    client: TestClient, session: Session
) -> None:
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
    update_global_settings(session, enabled_targets=ALL_TARGETS)
    media = upsert_tracked_media(
        session,
        file_path="/media/pilot.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
        instance_id=instance.id,
        sonarr_episode_id=101,
    )

    response = client.get(f"/api/files/{media.id}", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["node_type"] == "episode"
    assert body["tracked"] is True
    assert isinstance(body["library_node_id"], int)


def test_files_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    response = client.get(f"/api/files/{media.id}")
    assert response.status_code == 401
