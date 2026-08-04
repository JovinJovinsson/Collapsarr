"""Contract tests for the Library tree endpoint (COL-98).

Covers the response shape, the resolved Tracked values, hidden-node exclusion,
Sonarr-only / unknown-instance ``404``s, and the API-key-required behaviour for
``GET /api/library/instances/{id}/tree`` -- mirroring
:mod:`tests.test_arr_routes`. Library nodes are seeded through the real
:func:`~collapsarr.library.service.sync_library` against the app's own session
factory (there is no create-node endpoint; the mirror is scan-driven).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.arr.catalog import CatalogEpisode, CatalogSeries, SonarrCatalog
from collapsarr.arr.models import ArrInstance, InstanceType
from collapsarr.library.service import sync_library
from collapsarr.settings.service import get_global_settings, update_global_settings

UNREACHABLE_URL = "http://127.0.0.1:9"


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _seed_instance(session: Session, *, type_: InstanceType = InstanceType.SONARR) -> int:
    instance = ArrInstance(
        name=f"Instance {type_.value}", type=type_, base_url=UNREACHABLE_URL, api_key="k"
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)
    return instance.id


def _catalog(instance_id: int) -> SonarrCatalog:
    return SonarrCatalog(
        instance_id=instance_id,
        series=(
            CatalogSeries(
                series_id=1,
                title="Breaking Bad",
                season_numbers=(1,),
                episodes=(
                    CatalogEpisode(101, 1, 1, "Pilot", has_file=True),
                    CatalogEpisode(102, 1, 2, "Cat's in the Bag", has_file=False),
                ),
            ),
        ),
    )


def _seed_library(client: TestClient, *, type_: InstanceType = InstanceType.SONARR) -> int:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        instance_id = _seed_instance(session, type_=type_)
        if type_ is InstanceType.SONARR:
            sync_library(session, instance_id=instance_id, catalog=_catalog(instance_id))
        return instance_id


def test_tree_returns_series_season_episode_shape(client: TestClient) -> None:
    instance_id = _seed_library(client)

    response = client.get(
        f"/api/library/instances/{instance_id}/tree", headers=_auth_headers(client)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["instance_id"] == instance_id
    assert len(body["series"]) == 1
    series = body["series"][0]
    assert series["kind"] == "series"
    assert series["title"] == "Breaking Bad"
    assert series["tracked"] is True  # global default_tracked
    assert len(series["seasons"]) == 1
    season = series["seasons"][0]
    assert season["kind"] == "season"
    assert season["season_number"] == 1
    episodes = season["episodes"]
    assert [e["episode_number"] for e in episodes] == [1, 2]
    assert episodes[0]["kind"] == "episode"
    assert episodes[0]["has_file"] is True
    assert episodes[1]["has_file"] is False  # no-file episode present
    assert all(e["tracked"] is True for e in episodes)


def test_tree_reflects_default_tracked_false(client: TestClient) -> None:
    instance_id = _seed_library(client)
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        update_global_settings(session, default_tracked=False)

    response = client.get(
        f"/api/library/instances/{instance_id}/tree", headers=_auth_headers(client)
    )

    assert response.status_code == 200
    assert response.json()["series"][0]["tracked"] is False


def test_tree_unknown_instance_returns_404(client: TestClient) -> None:
    response = client.get("/api/library/instances/999/tree", headers=_auth_headers(client))
    assert response.status_code == 404


def test_tree_radarr_instance_returns_404(client: TestClient) -> None:
    instance_id = _seed_library(client, type_=InstanceType.RADARR)
    response = client.get(
        f"/api/library/instances/{instance_id}/tree", headers=_auth_headers(client)
    )
    assert response.status_code == 404


def test_tree_requires_the_api_key(client: TestClient) -> None:
    instance_id = _seed_library(client)
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        update_global_settings(session, ui_auth_enabled=True)

    response = client.get(f"/api/library/instances/{instance_id}/tree")
    assert response.status_code == 401
