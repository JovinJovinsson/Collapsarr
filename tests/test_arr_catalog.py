"""Tests for the Sonarr/Radarr full-catalog fetch (COL-98/COL-99).

Every case is driven by an ``httpx.MockTransport`` -- no live network call is
made. Unlike :mod:`tests.test_arr_files` (the monitored-only downmix fetch),
these assert the *full* catalog is returned: unmonitored series/movies and
not-yet-downloaded (``hasFile: false``) episodes/movies included.
"""

from __future__ import annotations

import httpx
import pytest

from collapsarr.arr.catalog import fetch_radarr_catalog, fetch_sonarr_catalog
from collapsarr.arr.models import ArrInstance, InstanceType

_SERIES_PAYLOAD = [
    {
        "id": 1,
        "title": "Breaking Bad",
        "monitored": True,
        "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
    },
    {
        # Unmonitored: must still appear in the full catalog.
        "id": 2,
        "title": "Unmonitored Show",
        "monitored": False,
        "seasons": [{"seasonNumber": 1}],
    },
]

_EPISODES_SERIES_1 = [
    {"id": 101, "seasonNumber": 1, "episodeNumber": 1, "title": "Pilot", "hasFile": True},
    # No file yet: must still appear so its Tracked preference can be set ahead.
    {"id": 102, "seasonNumber": 1, "episodeNumber": 2, "title": "Bag", "hasFile": False},
    {"id": 201, "seasonNumber": 2, "episodeNumber": 1, "title": "737", "hasFile": False},
]

_EPISODES_SERIES_2 = [
    {"id": 301, "seasonNumber": 1, "episodeNumber": 1, "title": "Ep One", "hasFile": False},
]


def _sonarr_instance() -> ArrInstance:
    return ArrInstance(
        id=7,
        name="Main Sonarr",
        type=InstanceType.SONARR,
        base_url="http://sonarr.local:8989",
        api_key="sonarr-api-key",
    )


def _catalog_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=_SERIES_PAYLOAD)
        if request.url.path == "/api/v3/episode":
            series_id = request.url.params.get("seriesId")
            if series_id == "1":
                return httpx.Response(200, json=_EPISODES_SERIES_1)
            if series_id == "2":
                return httpx.Response(200, json=_EPISODES_SERIES_2)
        raise AssertionError(f"unexpected request: {request.url}")

    return httpx.MockTransport(handler)


def test_fetch_returns_all_series_regardless_of_monitored() -> None:
    catalog = fetch_sonarr_catalog(_sonarr_instance(), transport=_catalog_transport())

    assert catalog.instance_id == 7
    titles = {series.title for series in catalog.series}
    assert titles == {"Breaking Bad", "Unmonitored Show"}


def test_fetch_includes_episodes_without_a_file() -> None:
    catalog = fetch_sonarr_catalog(_sonarr_instance(), transport=_catalog_transport())

    breaking_bad = next(s for s in catalog.series if s.series_id == 1)
    by_id = {episode.episode_id: episode for episode in breaking_bad.episodes}
    assert set(by_id) == {101, 102, 201}
    assert by_id[101].has_file is True
    assert by_id[102].has_file is False  # not-yet-downloaded, still present
    assert by_id[201].season_number == 2


def test_fetch_derives_season_numbers_from_series_and_episodes() -> None:
    catalog = fetch_sonarr_catalog(_sonarr_instance(), transport=_catalog_transport())

    breaking_bad = next(s for s in catalog.series if s.series_id == 1)
    assert breaking_bad.season_numbers == (1, 2)


def test_fetch_rejects_a_radarr_instance() -> None:
    radarr = ArrInstance(
        id=9,
        name="Main Radarr",
        type=InstanceType.RADARR,
        base_url="http://radarr.local:7878",
        api_key="radarr-api-key",
    )
    with pytest.raises(ValueError, match="Sonarr"):
        fetch_sonarr_catalog(radarr, transport=_catalog_transport())


def test_fetch_propagates_http_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        fetch_sonarr_catalog(_sonarr_instance(), transport=transport)


# --- Radarr (COL-99) ----------------------------------------------------------

_MOVIE_PAYLOAD = [
    {"id": 1, "title": "Arrival", "monitored": True, "hasFile": True},
    # Unmonitored: must still appear in the full catalog.
    {"id": 2, "title": "Unmonitored Movie", "monitored": False, "hasFile": False},
    # Monitored but no file yet: must still appear.
    {"id": 3, "title": "Not Yet Downloaded", "monitored": True, "hasFile": False},
]


def _radarr_instance() -> ArrInstance:
    return ArrInstance(
        id=9,
        name="Main Radarr",
        type=InstanceType.RADARR,
        base_url="http://radarr.local:7878",
        api_key="radarr-api-key",
    )


def _movie_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=_MOVIE_PAYLOAD)
        raise AssertionError(f"unexpected request: {request.url}")

    return httpx.MockTransport(handler)


def test_radarr_fetch_returns_all_movies_regardless_of_monitored() -> None:
    catalog = fetch_radarr_catalog(_radarr_instance(), transport=_movie_transport())

    assert catalog.instance_id == 9
    titles = {movie.title for movie in catalog.movies}
    assert titles == {"Arrival", "Unmonitored Movie", "Not Yet Downloaded"}


def test_radarr_fetch_includes_movies_without_a_file() -> None:
    catalog = fetch_radarr_catalog(_radarr_instance(), transport=_movie_transport())

    by_id = {movie.movie_id: movie for movie in catalog.movies}
    assert by_id[1].has_file is True
    assert by_id[2].has_file is False  # not-yet-downloaded, still present
    assert by_id[3].has_file is False


def test_radarr_fetch_rejects_a_sonarr_instance() -> None:
    with pytest.raises(ValueError, match="Radarr"):
        fetch_radarr_catalog(_sonarr_instance(), transport=_movie_transport())


def test_radarr_fetch_propagates_http_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        fetch_radarr_catalog(_radarr_instance(), transport=transport)
