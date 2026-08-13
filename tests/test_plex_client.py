"""Tests for the Plex HTTP client (COL-209).

Every case is driven by an ``httpx.MockTransport`` fed from recorded fixture
responses under ``tests/fixtures/plex/`` — no live network call is made,
matching ``test_arr_client.py``'s treatment of the Sonarr/Radarr client.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from collapsarr.plex.client import (
    PLEX_EPISODE_TYPE,
    AnalyzeResult,
    ConnectivityResult,
    LibrarySection,
    PlexMediaItem,
    SectionItemsResult,
    SectionsResult,
    analyze_item,
    check_connectivity,
    list_library_sections,
    list_section_items,
    search_items,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "plex"


def _load_fixture(name: str) -> dict[str, object]:
    payload: dict[str, object] = json.loads((FIXTURES_DIR / name).read_text())
    return payload


def _transport_returning(status_code: int, payload: object) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# check_connectivity
# ---------------------------------------------------------------------------


def test_identity_ok_reports_version() -> None:
    payload = _load_fixture("identity_ok.json")
    transport = _transport_returning(200, payload)

    result = check_connectivity("http://plex.local:32400", "plex-token", transport=transport)

    assert result == ConnectivityResult(ok=True, version="1.32.5.7349-8f4248874", error=None)


def test_connectivity_request_carries_token_header_and_json_accept() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"MediaContainer": {"version": "1.0.0"}})

    transport = httpx.MockTransport(handler)

    check_connectivity("http://plex.local:32400/", "plex-token", transport=transport)

    request = seen["request"]
    assert request.url.path == "/"
    assert request.headers["X-Plex-Token"] == "plex-token"
    assert request.headers["Accept"] == "application/json"


def test_connectivity_unauthorized_response_is_reported_as_failure() -> None:
    payload = _load_fixture("unauthorized.json")
    transport = _transport_returning(401, payload)

    result = check_connectivity("http://plex.local:32400", "wrong-token", transport=transport)

    assert result.ok is False
    assert result.version is None
    assert result.error is not None
    assert "401" in result.error


def test_connectivity_connection_error_is_reported_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    transport = httpx.MockTransport(handler)

    result = check_connectivity("http://unreachable.local:32400", "some-token", transport=transport)

    assert result.ok is False
    assert result.version is None
    assert result.error is not None


def test_connectivity_timeout_is_reported_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Timed out", request=request)

    transport = httpx.MockTransport(handler)

    result = check_connectivity("http://slow.local:32400", "some-token", transport=transport)

    assert result.ok is False
    assert result.error is not None


def test_connectivity_malformed_json_is_reported_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    transport = httpx.MockTransport(handler)

    result = check_connectivity("http://plex.local:32400", "some-token", transport=transport)

    assert result.ok is False
    assert result.error is not None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"MediaContainer": {}},
        {"MediaContainer": {"version": 123}},
        {"MediaContainer": {"version": ""}},
    ],
)
def test_connectivity_missing_or_invalid_version_is_reported_as_failure(
    payload: dict[str, object],
) -> None:
    transport = _transport_returning(200, payload)

    result = check_connectivity("http://plex.local:32400", "some-token", transport=transport)

    assert result.ok is False
    assert result.error is not None


def test_connectivity_empty_base_url_is_reported_as_failure_not_raised() -> None:
    """An unconfigured (blank) base URL fails gracefully, matching a fresh row."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    result = check_connectivity("", "some-token", transport=transport)

    assert result.ok is False
    assert result.error is not None


# ---------------------------------------------------------------------------
# analyze_item
# ---------------------------------------------------------------------------


def test_analyze_item_success() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200))

    result = analyze_item("http://plex.local:32400", "plex-token", "12345", transport=transport)

    assert result == AnalyzeResult(ok=True, error=None)


def test_analyze_item_request_carries_token_header_and_rating_key_path() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)

    analyze_item("http://plex.local:32400", "plex-token", "999", transport=transport)

    request = seen["request"]
    assert request.method == "PUT"
    assert request.url.path == "/library/metadata/999/analyze"
    assert request.headers["X-Plex-Token"] == "plex-token"


def test_analyze_item_unauthorized_is_reported_as_failure() -> None:
    payload = _load_fixture("unauthorized.json")
    transport = _transport_returning(401, payload)

    result = analyze_item("http://plex.local:32400", "wrong-token", "1", transport=transport)

    assert result.ok is False
    assert result.error is not None
    assert "401" in result.error


def test_analyze_item_not_found_is_reported_as_failure() -> None:
    transport = _transport_returning(404, {"errors": [{"code": 404, "message": "Not Found"}]})

    result = analyze_item(
        "http://plex.local:32400", "plex-token", "does-not-exist", transport=transport
    )

    assert result.ok is False
    assert result.error is not None


def test_analyze_item_connection_error_is_reported_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    transport = httpx.MockTransport(handler)

    result = analyze_item("http://unreachable.local:32400", "plex-token", "1", transport=transport)

    assert result.ok is False
    assert result.error is not None


# ---------------------------------------------------------------------------
# list_library_sections
# ---------------------------------------------------------------------------


def test_list_library_sections_returns_parsed_sections() -> None:
    payload = _load_fixture("sections_ok.json")
    transport = _transport_returning(200, payload)

    result = list_library_sections("http://plex.local:32400", "plex-token", transport=transport)

    assert result == SectionsResult(
        ok=True,
        sections=(
            LibrarySection(key="1", title="Movies", type="movie"),
            LibrarySection(key="2", title="TV Shows", type="show"),
        ),
        error=None,
    )


def test_list_library_sections_empty_directory_is_success_with_no_sections() -> None:
    payload = _load_fixture("sections_empty.json")
    transport = _transport_returning(200, payload)

    result = list_library_sections("http://plex.local:32400", "plex-token", transport=transport)

    assert result.ok is True
    assert result.sections == ()
    assert result.error is None


def test_list_library_sections_request_carries_token_header_and_json_accept() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"MediaContainer": {}})

    transport = httpx.MockTransport(handler)

    list_library_sections("http://plex.local:32400", "plex-token", transport=transport)

    request = seen["request"]
    assert request.url.path == "/library/sections"
    assert request.headers["X-Plex-Token"] == "plex-token"
    assert request.headers["Accept"] == "application/json"


def test_list_library_sections_unauthorized_is_reported_as_failure() -> None:
    payload = _load_fixture("unauthorized.json")
    transport = _transport_returning(401, payload)

    result = list_library_sections("http://plex.local:32400", "wrong-token", transport=transport)

    assert result.ok is False
    assert result.sections == ()
    assert result.error is not None
    assert "401" in result.error


def test_list_library_sections_malformed_json_is_reported_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    transport = httpx.MockTransport(handler)

    result = list_library_sections("http://plex.local:32400", "some-token", transport=transport)

    assert result.ok is False
    assert result.error is not None


def test_list_library_sections_missing_media_container_is_reported_as_failure() -> None:
    transport = _transport_returning(200, {"not_media_container": True})

    result = list_library_sections("http://plex.local:32400", "some-token", transport=transport)

    assert result.ok is False
    assert result.error is not None


def test_list_library_sections_skips_malformed_entries() -> None:
    payload = {
        "MediaContainer": {
            "Directory": [
                {"key": "1", "title": "Movies", "type": "movie"},
                {"key": "2", "title": "Missing type"},
                "not-a-dict",
            ]
        }
    }
    transport = _transport_returning(200, payload)

    result = list_library_sections("http://plex.local:32400", "some-token", transport=transport)

    assert result.ok is True
    assert result.sections == (LibrarySection(key="1", title="Movies", type="movie"),)


def test_list_library_sections_connection_error_is_reported_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    transport = httpx.MockTransport(handler)

    result = list_library_sections(
        "http://unreachable.local:32400", "some-token", transport=transport
    )

    assert result.ok is False
    assert result.error is not None


# ---------------------------------------------------------------------------
# list_section_items (COL-210)
# ---------------------------------------------------------------------------

_MOVIES_PAYLOAD = {
    "MediaContainer": {
        "size": 2,
        "Metadata": [
            {
                "ratingKey": "101",
                "type": "movie",
                "title": "Blade Runner",
                "Media": [{"Part": [{"file": "/movies/Blade Runner (1982)/br.mkv"}]}],
            },
            {
                "ratingKey": 102,  # Plex sometimes serialises ratingKey as an int
                "type": "movie",
                "title": "Dune",
                "Media": [
                    {"Part": [{"file": "/movies/Dune (2021)/dune-cd1.mkv"}]},
                    {"Part": [{"file": "/movies/Dune (2021)/dune-cd2.mkv"}]},
                ],
            },
        ],
    }
}

_EPISODES_PAYLOAD = {
    "MediaContainer": {
        "Metadata": [
            {
                "ratingKey": "555",
                "type": "episode",
                "title": "Winter Is Coming",
                "grandparentTitle": "Game of Thrones",
                "parentIndex": 1,
                "index": 1,
                "Media": [{"Part": [{"file": "/tv/GoT/Season 01/s01e01.mkv"}]}],
            }
        ]
    }
}


def test_list_section_items_movies_returns_rating_keys_and_file_paths() -> None:
    transport = _transport_returning(200, _MOVIES_PAYLOAD)

    result = list_section_items("http://plex.local:32400", "plex-token", "1", transport=transport)

    assert result.ok is True
    assert result.items == (
        PlexMediaItem(
            rating_key="101",
            file_paths=("/movies/Blade Runner (1982)/br.mkv",),
            section_key="1",
            title="Blade Runner",
            type="movie",
        ),
        PlexMediaItem(
            rating_key="102",
            file_paths=(
                "/movies/Dune (2021)/dune-cd1.mkv",
                "/movies/Dune (2021)/dune-cd2.mkv",
            ),
            section_key="1",
            title="Dune",
            type="movie",
        ),
    )


def test_list_section_items_episodes_carry_season_and_episode_scope() -> None:
    transport = _transport_returning(200, _EPISODES_PAYLOAD)

    result = list_section_items(
        "http://plex.local:32400",
        "plex-token",
        "2",
        item_type=PLEX_EPISODE_TYPE,
        transport=transport,
    )

    assert result.items == (
        PlexMediaItem(
            rating_key="555",
            file_paths=("/tv/GoT/Season 01/s01e01.mkv",),
            section_key="2",
            title="Winter Is Coming",
            grandparent_title="Game of Thrones",
            season_number=1,
            episode_number=1,
            type="episode",
        ),
    )


def test_list_section_items_passes_the_type_param_for_shows() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"MediaContainer": {"Metadata": []}})

    transport = httpx.MockTransport(handler)

    list_section_items(
        "http://plex.local:32400",
        "plex-token",
        "2",
        item_type=PLEX_EPISODE_TYPE,
        transport=transport,
    )

    request = seen["request"]
    assert request.url.path == "/library/sections/2/all"
    assert request.url.params.get("type") == "4"
    assert request.headers["X-Plex-Token"] == "plex-token"
    assert request.headers["Accept"] == "application/json"


def test_list_section_items_no_type_param_for_movies() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"MediaContainer": {"Metadata": []}})

    transport = httpx.MockTransport(handler)

    list_section_items("http://plex.local:32400", "plex-token", "1", transport=transport)

    assert "type" not in seen["request"].url.params


def test_list_section_items_empty_metadata_is_success_with_no_items() -> None:
    transport = _transport_returning(200, {"MediaContainer": {"size": 0}})

    result = list_section_items("http://plex.local:32400", "plex-token", "1", transport=transport)

    assert result == SectionItemsResult(ok=True, items=(), error=None)


def test_list_section_items_skips_entries_without_a_rating_key() -> None:
    payload = {
        "MediaContainer": {
            "Metadata": [
                {"ratingKey": "1", "title": "Kept", "Media": [{"Part": [{"file": "/a.mkv"}]}]},
                {"title": "No ratingKey"},
                "not-a-dict",
            ]
        }
    }
    transport = _transport_returning(200, payload)

    result = list_section_items("http://plex.local:32400", "plex-token", "1", transport=transport)

    assert [item.rating_key for item in result.items] == ["1"]


def test_list_section_items_item_without_media_has_empty_file_paths() -> None:
    payload = {"MediaContainer": {"Metadata": [{"ratingKey": "9", "title": "No file yet"}]}}
    transport = _transport_returning(200, payload)

    result = list_section_items("http://plex.local:32400", "plex-token", "1", transport=transport)

    assert result.items[0].file_paths == ()


def test_list_section_items_unauthorized_is_reported_as_failure() -> None:
    payload = _load_fixture("unauthorized.json")
    transport = _transport_returning(401, payload)

    result = list_section_items("http://plex.local:32400", "wrong-token", "1", transport=transport)

    assert result.ok is False
    assert result.items == ()
    assert result.error is not None
    assert "401" in result.error


def test_list_section_items_connection_error_is_reported_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    transport = httpx.MockTransport(handler)

    result = list_section_items(
        "http://unreachable.local:32400", "some-token", "1", transport=transport
    )

    assert result.ok is False
    assert result.error is not None


def test_list_section_items_empty_base_url_is_reported_as_failure_not_raised() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200))

    result = list_section_items("", "some-token", "1", transport=transport)

    assert result.ok is False
    assert result.error is not None


# ---------------------------------------------------------------------------
# search_items (COL-210) -- the single live scoped fallback query
# ---------------------------------------------------------------------------


def test_search_items_issues_one_query_and_derives_section_from_result() -> None:
    seen: list[httpx.Request] = []
    payload = {
        "MediaContainer": {
            "Metadata": [
                {
                    "ratingKey": "700",
                    "type": "episode",
                    "title": "The Rains of Castamere",
                    "grandparentTitle": "Game of Thrones",
                    "parentIndex": 3,
                    "index": 9,
                    "librarySectionID": 2,
                    "Media": [{"Part": [{"file": "/tv/GoT/Season 03/s03e09.mkv"}]}],
                }
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    result = search_items(
        "http://plex.local:32400", "plex-token", "Game of Thrones", transport=transport
    )

    assert len(seen) == 1
    assert seen[0].url.path == "/search"
    assert seen[0].url.params.get("query") == "Game of Thrones"
    assert result.items == (
        PlexMediaItem(
            rating_key="700",
            file_paths=("/tv/GoT/Season 03/s03e09.mkv",),
            section_key="2",
            title="The Rains of Castamere",
            grandparent_title="Game of Thrones",
            season_number=3,
            episode_number=9,
            type="episode",
        ),
    )


def test_search_items_empty_result_is_success_with_no_items() -> None:
    transport = _transport_returning(200, {"MediaContainer": {"size": 0}})

    result = search_items("http://plex.local:32400", "plex-token", "Nothing", transport=transport)

    assert result == SectionItemsResult(ok=True, items=(), error=None)


def test_search_items_connection_error_is_reported_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    transport = httpx.MockTransport(handler)

    result = search_items(
        "http://unreachable.local:32400", "plex-token", "Dune", transport=transport
    )

    assert result.ok is False
    assert result.error is not None
