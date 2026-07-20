"""Tests for the monitored-file-list client (COL-12).

Every case is driven by an ``httpx.MockTransport`` fed from recorded fixture
responses under ``tests/fixtures/arr/`` — no live network call is made.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from collapsarr.arr.files import AudioInfo, MonitoredFile, fetch_monitored_files
from collapsarr.arr.models import ArrInstance, InstanceType

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arr"


def _load_fixture(name: str) -> object:
    payload: object = json.loads((FIXTURES_DIR / name).read_text())
    return payload


def _sonarr_instance() -> ArrInstance:
    return ArrInstance(
        id=7,
        name="Main Sonarr",
        type=InstanceType.SONARR,
        base_url="http://sonarr.local:8989",
        api_key="sonarr-api-key",
    )


def _radarr_instance() -> ArrInstance:
    return ArrInstance(
        id=9,
        name="Main Radarr",
        type=InstanceType.RADARR,
        base_url="http://radarr.local:7878",
        api_key="radarr-api-key",
    )


def _sonarr_transport() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    series_payload = _load_fixture("sonarr_series_list.json")
    episodefiles_payload = _load_fixture("sonarr_episodefiles_series1.json")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=series_payload)
        if request.url.path == "/api/v3/episodefile":
            assert request.url.params.get("seriesId") == "1", (
                "should only fetch episode files for the monitored series"
            )
            return httpx.Response(200, json=episodefiles_payload)
        raise AssertionError(f"unexpected request: {request.url}")

    return httpx.MockTransport(handler), seen


def _radarr_transport() -> httpx.MockTransport:
    payload = _load_fixture("radarr_movie_list.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/movie"
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_fetch_sonarr_monitored_files_returns_normalized_list() -> None:
    """Only files belonging to monitored series are returned, normalized."""
    transport, _ = _sonarr_transport()

    files = fetch_monitored_files(_sonarr_instance(), transport=transport)

    assert files == [
        MonitoredFile(
            instance_id=7,
            media_title="Breaking Bad",
            file_path="/tv/Breaking Bad/Season 01/Breaking Bad - S01E01 - Pilot.mkv",
            source_file_id=101,
            audio=AudioInfo(codec="AC3", channels=5.1, languages="eng", stream_count=1),
        ),
        MonitoredFile(
            instance_id=7,
            media_title="Breaking Bad",
            file_path="/tv/Breaking Bad/Season 01/Breaking Bad - S01E02 - Cats in the Bag.mkv",
            source_file_id=102,
            audio=AudioInfo(codec="AAC", channels=2.0, languages="eng", stream_count=1),
        ),
    ]


def test_fetch_sonarr_never_queries_unmonitored_series() -> None:
    """The unmonitored series' episode files are never fetched (no extra request)."""
    transport, seen = _sonarr_transport()

    fetch_monitored_files(_sonarr_instance(), transport=transport)

    assert len(seen) == 2  # one /series call, one /episodefile call (series 1 only)
    paths = [request.url.path for request in seen]
    assert paths == ["/api/v3/series", "/api/v3/episodefile"]


def test_fetch_sonarr_request_carries_api_key_header() -> None:
    """Requests to both endpoints carry the instance's X-Api-Key header."""
    transport, seen = _sonarr_transport()

    fetch_monitored_files(_sonarr_instance(), transport=transport)

    assert all(request.headers["X-Api-Key"] == "sonarr-api-key" for request in seen)


def test_fetch_radarr_monitored_files_returns_normalized_list() -> None:
    """Only monitored movies with a downloaded file are returned, normalized.

    Excludes: a monitored movie with no file yet, and an unmonitored movie
    that does have a file. Includes a movie whose file has no mediaInfo block
    at all, verifying that yields ``audio=None`` rather than an error.
    """
    transport = _radarr_transport()

    files = fetch_monitored_files(_radarr_instance(), transport=transport)

    assert files == [
        MonitoredFile(
            instance_id=9,
            media_title="Interstellar",
            file_path="/movies/Interstellar (2014)/Interstellar (2014) Bluray-1080p.mkv",
            source_file_id=501,
            audio=AudioInfo(codec="DTS-HD MA", channels=7.1, languages="eng", stream_count=2),
        ),
        MonitoredFile(
            instance_id=9,
            media_title="Silent Film",
            file_path="/movies/Silent Film (1921)/Silent Film (1921) DVD.mkv",
            source_file_id=504,
            audio=None,
        ),
    ]


def test_fetch_radarr_request_carries_api_key_header() -> None:
    """The request to /api/v3/movie carries the instance's X-Api-Key header."""
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json=_load_fixture("radarr_movie_list.json"))

    transport = httpx.MockTransport(handler)

    fetch_monitored_files(_radarr_instance(), transport=transport)

    assert seen["request"].headers["X-Api-Key"] == "radarr-api-key"


def test_fetch_monitored_files_raises_on_http_error() -> None:
    """A non-2xx response propagates as an httpx error rather than an empty list.

    Unlike check_connectivity, this function does not swallow failures — a
    failed fetch must be distinguishable from "genuinely no monitored files".
    """
    payload = _load_fixture("unauthorized.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=payload)

    transport = httpx.MockTransport(handler)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_monitored_files(_sonarr_instance(), transport=transport)


def test_fetch_monitored_files_raises_on_connection_error() -> None:
    """A transport-level failure propagates rather than returning an empty list."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    transport = httpx.MockTransport(handler)

    with pytest.raises(httpx.ConnectError):
        fetch_monitored_files(_radarr_instance(), transport=transport)


def test_fetch_monitored_files_returns_empty_list_when_nothing_monitored() -> None:
    """An instance with no monitored+downloaded media yields an empty list."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)

    assert fetch_monitored_files(_radarr_instance(), transport=transport) == []
