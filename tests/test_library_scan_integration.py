"""End-to-end integration test for the scan -> Library -> tree-API chain (COL-98/COL-99).

Mirrors :mod:`tests.test_wanted_pipeline_integration`: drives a **real**
:class:`~collapsarr.jobs.scheduler.JobScheduler` scan, with only the arr HTTP
boundary stubbed (an injected ``catalog_fetch``/``radarr_catalog_fetch``
standing in for :func:`~collapsarr.arr.catalog.fetch_sonarr_catalog` /
:func:`~collapsarr.arr.catalog.fetch_radarr_catalog`, the same seam the wanted
test stubs ``probe`` at). Assertions read the resulting tree through the real
``GET /api/library/instances/{id}/tree`` HTTP endpoint -- the thing this
ticket's acceptance criteria are stated in terms of -- not mocks.

The monitored-file downmix pass of the same scan targets the seeded instance's
unreachable URL and is swallowed by the scheduler; the Library pass under test
uses the injected catalog fetch and is unaffected.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.arr.catalog import (
    CatalogEpisode,
    CatalogMovie,
    CatalogSeries,
    RadarrCatalog,
    SonarrCatalog,
)
from collapsarr.arr.models import ArrInstance, InstanceType
from collapsarr.config import Settings
from collapsarr.jobs.queue import JobQueue
from collapsarr.jobs.scheduler import JobScheduler
from collapsarr.settings.service import get_global_settings

UNREACHABLE_URL = "http://127.0.0.1:9"


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _seed_sonarr_instance(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        instance = ArrInstance(
            name="Main Sonarr", type=InstanceType.SONARR, base_url=UNREACHABLE_URL, api_key="k"
        )
        session.add(instance)
        session.commit()
        session.refresh(instance)
        return instance.id


def _seed_radarr_instance(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        instance = ArrInstance(
            name="Main Radarr", type=InstanceType.RADARR, base_url=UNREACHABLE_URL, api_key="k"
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


def test_a_real_scan_mirrors_the_catalog_into_the_tree_api(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_sonarr_instance(session_factory)

    fetched: list[int] = []

    def catalog_fetch(instance: ArrInstance) -> SonarrCatalog:
        fetched.append(instance.id)
        return _catalog(instance.id)

    queue = JobQueue.from_settings(settings)
    scheduler = JobScheduler(queue, session_factory, settings, catalog_fetch=catalog_fetch)

    scheduler.scan_once()

    assert fetched == [instance_id]  # the real scan drove the injected fetch

    response = client.get(
        f"/api/library/instances/{instance_id}/tree", headers=_auth_headers(client)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    series = body["series"]
    assert len(series) == 1
    assert series[0]["title"] == "Breaking Bad"
    episodes = series[0]["seasons"][0]["episodes"]
    assert {e["sonarr_episode_id"] for e in episodes} == {101, 102}
    # Full catalog incl. the not-yet-downloaded episode, tracked by default.
    assert all(e["tracked"] is True for e in episodes)


def test_a_second_scan_soft_hides_a_removed_node_from_the_tree_api(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_sonarr_instance(session_factory)

    catalogs = iter(
        [
            _catalog(instance_id),
            # Second scan drops episode 102.
            SonarrCatalog(
                instance_id=instance_id,
                series=(
                    CatalogSeries(
                        series_id=1,
                        title="Breaking Bad",
                        season_numbers=(1,),
                        episodes=(CatalogEpisode(101, 1, 1, "Pilot", has_file=True),),
                    ),
                ),
            ),
        ]
    )

    def catalog_fetch(_instance: ArrInstance) -> SonarrCatalog:
        return next(catalogs)

    queue = JobQueue.from_settings(settings)
    scheduler = JobScheduler(queue, session_factory, settings, catalog_fetch=catalog_fetch)

    scheduler.scan_once()
    scheduler.scan_once()

    response = client.get(
        f"/api/library/instances/{instance_id}/tree", headers=_auth_headers(client)
    )
    assert response.status_code == 200
    episodes = response.json()["series"][0]["seasons"][0]["episodes"]
    assert {e["sonarr_episode_id"] for e in episodes} == {101}  # 102 soft-hidden


# --- Radarr (COL-99) ----------------------------------------------------------


def _radarr_catalog(instance_id: int) -> RadarrCatalog:
    return RadarrCatalog(
        instance_id=instance_id,
        movies=(
            CatalogMovie(movie_id=1, title="Arrival", has_file=True),
            CatalogMovie(movie_id=2, title="Not Yet Downloaded", has_file=False),
        ),
    )


def test_a_real_scan_mirrors_the_radarr_catalog_into_the_tree_api(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_radarr_instance(session_factory)

    fetched: list[int] = []

    def radarr_catalog_fetch(instance: ArrInstance) -> RadarrCatalog:
        fetched.append(instance.id)
        return _radarr_catalog(instance.id)

    queue = JobQueue.from_settings(settings)
    scheduler = JobScheduler(
        queue, session_factory, settings, radarr_catalog_fetch=radarr_catalog_fetch
    )

    scheduler.scan_once()

    assert fetched == [instance_id]  # the real scan drove the injected fetch

    response = client.get(
        f"/api/library/instances/{instance_id}/tree", headers=_auth_headers(client)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    movies = body["movies"]
    assert {m["title"] for m in movies} == {"Arrival", "Not Yet Downloaded"}
    # Full catalog incl. the not-yet-downloaded movie, tracked by default.
    assert all(m["tracked"] is True for m in movies)


def test_a_second_scan_soft_hides_a_removed_movie_from_the_tree_api(
    settings: Settings, client: TestClient
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    instance_id = _seed_radarr_instance(session_factory)

    catalogs = iter(
        [
            _radarr_catalog(instance_id),
            # Second scan drops movie 2.
            RadarrCatalog(
                instance_id=instance_id,
                movies=(CatalogMovie(movie_id=1, title="Arrival", has_file=True),),
            ),
        ]
    )

    def radarr_catalog_fetch(_instance: ArrInstance) -> RadarrCatalog:
        return next(catalogs)

    queue = JobQueue.from_settings(settings)
    scheduler = JobScheduler(
        queue, session_factory, settings, radarr_catalog_fetch=radarr_catalog_fetch
    )

    scheduler.scan_once()
    scheduler.scan_once()

    response = client.get(
        f"/api/library/instances/{instance_id}/tree", headers=_auth_headers(client)
    )
    assert response.status_code == 200
    movies = response.json()["movies"]
    assert {m["radarr_movie_id"] for m in movies} == {1}  # movie 2 soft-hidden
