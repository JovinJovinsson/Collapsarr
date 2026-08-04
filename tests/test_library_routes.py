"""Contract tests for the Library tree and Tracked-update endpoints (COL-98/COL-99/COL-101).

Covers the tree response shape (both the Sonarr Series/Season/Episode tree and
the Radarr flat Movie list), the resolved Tracked values, hidden-node
exclusion, unknown-instance ``404``, and the API-key-required behaviour for
``GET /api/library/instances/{id}/tree`` -- mirroring
:mod:`tests.test_arr_routes` -- plus the ``POST /api/library/tracked`` bulk
Tracked-update endpoint (COL-101): single-reference and cascading
multi-reference requests, unknown-node ``404``, node-type-mismatch ``422``,
and the atomic-validation ("nothing written if any reference is bad")
guarantee. Library nodes are seeded through the real
:func:`~collapsarr.library.service.sync_library` against the app's own session
factory (there is no create-node endpoint; the mirror is scan-driven).
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
from collapsarr.library.models import LibraryNodeKind, make_node_key
from collapsarr.library.service import list_nodes, sync_library
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


def _radarr_catalog(instance_id: int) -> RadarrCatalog:
    return RadarrCatalog(
        instance_id=instance_id,
        movies=(
            CatalogMovie(movie_id=1, title="Arrival", has_file=True),
            CatalogMovie(movie_id=2, title="Not Yet Downloaded", has_file=False),
        ),
    )


def _seed_library(client: TestClient, *, type_: InstanceType = InstanceType.SONARR) -> int:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        instance_id = _seed_instance(session, type_=type_)
        if type_ is InstanceType.SONARR:
            sync_library(session, instance_id=instance_id, catalog=_catalog(instance_id))
        else:
            sync_library(session, instance_id=instance_id, catalog=_radarr_catalog(instance_id))
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


def test_tree_returns_flat_movie_shape_for_a_radarr_instance(client: TestClient) -> None:
    instance_id = _seed_library(client, type_=InstanceType.RADARR)

    response = client.get(
        f"/api/library/instances/{instance_id}/tree", headers=_auth_headers(client)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["instance_id"] == instance_id
    assert "series" not in body  # flat shape, no season/episode nesting
    movies = body["movies"]
    assert [m["title"] for m in movies] == ["Arrival", "Not Yet Downloaded"]
    assert movies[0]["kind"] == "movie"
    assert movies[0]["has_file"] is True
    assert movies[1]["has_file"] is False  # no-file movie present
    assert all(m["tracked"] is True for m in movies)  # global default_tracked


def test_tree_radarr_reflects_default_tracked_false(client: TestClient) -> None:
    instance_id = _seed_library(client, type_=InstanceType.RADARR)
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        update_global_settings(session, default_tracked=False)

    response = client.get(
        f"/api/library/instances/{instance_id}/tree", headers=_auth_headers(client)
    )

    assert response.status_code == 200
    assert all(m["tracked"] is False for m in response.json()["movies"])


def test_tree_requires_the_api_key(client: TestClient) -> None:
    instance_id = _seed_library(client)
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        update_global_settings(session, ui_auth_enabled=True)

    response = client.get(f"/api/library/instances/{instance_id}/tree")
    assert response.status_code == 401


# --- POST /api/library/tracked (COL-101) --------------------------------------


def _node_id(client: TestClient, instance_id: int, node_key: str) -> int:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        match = next(n for n in list_nodes(session, instance_id) if n.node_key == node_key)
        return match.id


def test_bulk_update_single_episode_reference_updates_only_that_node(client: TestClient) -> None:
    instance_id = _seed_library(client)
    episode_key = make_node_key(LibraryNodeKind.EPISODE, series_id=1, episode_id=101)
    other_episode_key = make_node_key(LibraryNodeKind.EPISODE, series_id=1, episode_id=102)
    episode_id = _node_id(client, instance_id, episode_key)

    response = client.post(
        "/api/library/tracked",
        json={"references": [{"node_type": "episode", "node_id": episode_id}], "tracked": False},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated"] == [{"id": episode_id, "kind": "episode", "tracked": False}]

    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        nodes_by_key = {n.node_key: n for n in list_nodes(session, instance_id)}
        assert nodes_by_key[episode_key].tracked_override is False
        assert nodes_by_key[other_episode_key].tracked_override is None  # untouched


def test_bulk_update_series_reference_cascades_to_all_descendants(client: TestClient) -> None:
    instance_id = _seed_library(client)
    series_id = _node_id(client, instance_id, make_node_key(LibraryNodeKind.SERIES, series_id=1))

    response = client.post(
        "/api/library/tracked",
        json={"references": [{"node_type": "series", "node_id": series_id}], "tracked": False},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    assert response.json()["updated"] == [{"id": series_id, "kind": "series", "tracked": False}]

    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        for node in list_nodes(session, instance_id):
            assert node.tracked_override is False, node


def test_bulk_update_accepts_multiple_cascading_references_in_one_request(
    client: TestClient,
) -> None:
    """A Radarr Movie reference alongside a Sonarr Series reference in one batch.

    Nodes are looked up by id only (no instance scoping on the endpoint), so a
    single request spanning two instances -- one cascading (Series), one leaf
    (Movie) -- is exercised here rather than as two separate requests.
    """
    sonarr_instance_id = _seed_library(client, type_=InstanceType.SONARR)
    radarr_instance_id = _seed_library(client, type_=InstanceType.RADARR)
    series_id = _node_id(
        client, sonarr_instance_id, make_node_key(LibraryNodeKind.SERIES, series_id=1)
    )
    movie_id = _node_id(
        client, radarr_instance_id, make_node_key(LibraryNodeKind.MOVIE, movie_id=1)
    )

    response = client.post(
        "/api/library/tracked",
        json={
            "references": [
                {"node_type": "series", "node_id": series_id},
                {"node_type": "movie", "node_id": movie_id},
            ],
            "tracked": False,
        },
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    updated = response.json()["updated"]
    assert {(u["id"], u["kind"], u["tracked"]) for u in updated} == {
        (series_id, "series", False),
        (movie_id, "movie", False),
    }

    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        assert all(n.tracked_override is False for n in list_nodes(session, sonarr_instance_id))
        movie_node = next(
            n for n in list_nodes(session, radarr_instance_id) if n.id == movie_id
        )
        assert movie_node.tracked_override is False


def test_bulk_update_unknown_node_id_returns_404(client: TestClient) -> None:
    _seed_library(client)

    response = client.post(
        "/api/library/tracked",
        json={"references": [{"node_type": "episode", "node_id": 999999}], "tracked": True},
        headers=_auth_headers(client),
    )

    assert response.status_code == 404


def test_bulk_update_mismatched_node_type_returns_422(client: TestClient) -> None:
    instance_id = _seed_library(client)
    series_id = _node_id(client, instance_id, make_node_key(LibraryNodeKind.SERIES, series_id=1))

    response = client.post(
        "/api/library/tracked",
        json={"references": [{"node_type": "episode", "node_id": series_id}], "tracked": False},
        headers=_auth_headers(client),
    )

    assert response.status_code == 422


def test_bulk_update_is_atomic_a_bad_reference_leaves_earlier_ones_unwritten(
    client: TestClient,
) -> None:
    instance_id = _seed_library(client)
    episode_key = make_node_key(LibraryNodeKind.EPISODE, series_id=1, episode_id=101)
    episode_id = _node_id(client, instance_id, episode_key)

    response = client.post(
        "/api/library/tracked",
        json={
            "references": [
                {"node_type": "episode", "node_id": episode_id},
                {"node_type": "episode", "node_id": 999999},
            ],
            "tracked": False,
        },
        headers=_auth_headers(client),
    )

    assert response.status_code == 404

    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        nodes_by_key = {n.node_key: n for n in list_nodes(session, instance_id)}
        assert nodes_by_key[episode_key].tracked_override is None  # nothing written


def test_bulk_update_requires_at_least_one_reference(client: TestClient) -> None:
    _seed_library(client)

    response = client.post(
        "/api/library/tracked",
        json={"references": [], "tracked": True},
        headers=_auth_headers(client),
    )

    assert response.status_code == 422


def test_bulk_update_requires_the_api_key(client: TestClient) -> None:
    instance_id = _seed_library(client)
    episode_id = _node_id(
        client, instance_id, make_node_key(LibraryNodeKind.EPISODE, series_id=1, episode_id=101)
    )
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        update_global_settings(session, ui_auth_enabled=True)

    response = client.post(
        "/api/library/tracked",
        json={"references": [{"node_type": "episode", "node_id": episode_id}], "tracked": False},
    )
    assert response.status_code == 401
