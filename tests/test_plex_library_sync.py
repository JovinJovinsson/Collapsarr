"""Tests for the Plex Library Item mapping table: wholesale rebuild + resolution (COL-210).

The Plex client calls are stubbed via the injectable ``list_sections``/
``list_items``/``search`` seams (in-memory fakes), not ``httpx.MockTransport`` --
the sync/resolution *logic* is what's under test here, independent of the HTTP
client (which has its own coverage in ``test_plex_client.py``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from collapsarr.library.models import LibraryNode, LibraryNodeKind, make_node_key
from collapsarr.media.models import TrackedMediaFile
from collapsarr.plex.client import (
    LibrarySection,
    PlexMediaItem,
    SectionItemsResult,
    SectionsResult,
)
from collapsarr.plex.library_sync import rebuild_library_items, resolve_rating_key
from collapsarr.plex.models import PlexLibraryItem

BASE_URL = "http://plex.local:32400"
TOKEN = "plex-token"


# --------------------------------------------------------------------------- #
# seam fakes
# --------------------------------------------------------------------------- #
def _sections(*sections: LibrarySection, ok: bool = True) -> Callable[..., SectionsResult]:
    def fn(base_url: str, token: str, *, transport: object | None = None) -> SectionsResult:
        return SectionsResult(ok=ok, sections=tuple(sections), error=None if ok else "boom")

    return fn


def _items(
    by_section: dict[str, Sequence[PlexMediaItem]],
    *,
    failing: frozenset[str] = frozenset(),
    record: list[tuple[str, str | None]] | None = None,
) -> Callable[..., SectionItemsResult]:
    def fn(
        base_url: str,
        token: str,
        section_key: str,
        *,
        item_type: str | None = None,
        transport: object | None = None,
    ) -> SectionItemsResult:
        if record is not None:
            record.append((section_key, item_type))
        if section_key in failing:
            return SectionItemsResult(ok=False, error="section unavailable")
        return SectionItemsResult(ok=True, items=tuple(by_section.get(section_key, ())))

    return fn


def _search(
    result: SectionItemsResult,
    *,
    record: list[str] | None = None,
    raises: bool = False,
) -> Callable[..., SectionItemsResult]:
    def fn(
        base_url: str, token: str, query: str, *, transport: object | None = None
    ) -> SectionItemsResult:
        if record is not None:
            record.append(query)
        if raises:
            raise RuntimeError("plex exploded")
        return result

    return fn


def _rows(session: Session) -> dict[str, tuple[str, str]]:
    return {
        row.file_path: (row.rating_key, row.section_key)
        for row in session.scalars(select(PlexLibraryItem))
    }


# --------------------------------------------------------------------------- #
# rebuild_library_items
# --------------------------------------------------------------------------- #
def test_rebuild_walks_all_sections_and_maps_every_file_path(session: Session) -> None:
    sections = _sections(
        LibrarySection(key="1", title="Movies", type="movie"),
        LibrarySection(key="2", title="TV", type="show"),
    )
    items = _items(
        {
            "1": [PlexMediaItem(rating_key="101", file_paths=("/movies/a.mkv",), type="movie")],
            "2": [
                PlexMediaItem(
                    rating_key="201",
                    file_paths=("/tv/s01e01.mkv", "/tv/s01e01-pt2.mkv"),
                    type="episode",
                )
            ],
        }
    )

    count = rebuild_library_items(
        session, base_url=BASE_URL, token=TOKEN, list_sections=sections, list_items=items
    )

    assert count == 3
    assert _rows(session) == {
        "/movies/a.mkv": ("101", "1"),
        "/tv/s01e01.mkv": ("201", "2"),
        "/tv/s01e01-pt2.mkv": ("201", "2"),
    }


def test_rebuild_passes_episode_type_for_show_sections_only(session: Session) -> None:
    record: list[tuple[str, str | None]] = []
    sections = _sections(
        LibrarySection(key="1", title="Movies", type="movie"),
        LibrarySection(key="2", title="TV", type="show"),
    )
    items = _items({}, record=record)

    rebuild_library_items(
        session, base_url=BASE_URL, token=TOKEN, list_sections=sections, list_items=items
    )

    assert record == [("1", None), ("2", "4")]


def test_rebuild_is_wholesale_replacing_stale_rows(session: Session) -> None:
    session.add(PlexLibraryItem(file_path="/old/gone.mkv", rating_key="999", section_key="1"))
    session.commit()

    sections = _sections(LibrarySection(key="1", title="Movies", type="movie"))
    items = _items(
        {"1": [PlexMediaItem(rating_key="101", file_paths=("/movies/new.mkv",), type="movie")]}
    )

    rebuild_library_items(
        session, base_url=BASE_URL, token=TOKEN, list_sections=sections, list_items=items
    )

    assert _rows(session) == {"/movies/new.mkv": ("101", "1")}


def test_rebuild_blank_base_url_is_a_noop_that_preserves_existing_rows(session: Session) -> None:
    session.add(PlexLibraryItem(file_path="/keep.mkv", rating_key="5", section_key="1"))
    session.commit()

    count = rebuild_library_items(
        session,
        base_url="",
        token=TOKEN,
        list_sections=_sections(LibrarySection(key="1", title="M", type="movie")),
        list_items=_items({}),
    )

    assert count == 0
    assert _rows(session) == {"/keep.mkv": ("5", "1")}


def test_rebuild_failed_sections_listing_preserves_existing_rows(session: Session) -> None:
    session.add(PlexLibraryItem(file_path="/keep.mkv", rating_key="5", section_key="1"))
    session.commit()

    count = rebuild_library_items(
        session,
        base_url=BASE_URL,
        token=TOKEN,
        list_sections=_sections(ok=False),
        list_items=_items({}),
    )

    assert count == 0
    assert _rows(session) == {"/keep.mkv": ("5", "1")}


def test_rebuild_skips_a_failing_section_but_keeps_the_others(session: Session) -> None:
    sections = _sections(
        LibrarySection(key="1", title="Movies", type="movie"),
        LibrarySection(key="2", title="TV", type="show"),
    )
    items = _items(
        {"1": [PlexMediaItem(rating_key="101", file_paths=("/movies/a.mkv",), type="movie")]},
        failing=frozenset({"2"}),
    )

    count = rebuild_library_items(
        session, base_url=BASE_URL, token=TOKEN, list_sections=sections, list_items=items
    )

    assert count == 1
    assert _rows(session) == {"/movies/a.mkv": ("101", "1")}


def test_rebuild_every_section_failing_preserves_existing_rows(session: Session) -> None:
    """Sections list successfully, but every one's item-fetch fails: not a
    truthful empty rebuild -- must not silently wipe a previously-good map
    (code-review must-fix)."""
    session.add(PlexLibraryItem(file_path="/keep.mkv", rating_key="5", section_key="1"))
    session.commit()

    sections = _sections(
        LibrarySection(key="1", title="Movies", type="movie"),
        LibrarySection(key="2", title="TV", type="show"),
    )
    items = _items({}, failing=frozenset({"1", "2"}))

    count = rebuild_library_items(
        session, base_url=BASE_URL, token=TOKEN, list_sections=sections, list_items=items
    )

    assert count == 0
    assert _rows(session) == {"/keep.mkv": ("5", "1")}


def test_rebuild_no_sections_configured_is_a_truthful_empty_commit(session: Session) -> None:
    """Zero sections configured (nothing failed) is legitimately empty -- unlike
    every-section-failing, this *does* commit an empty table."""
    session.add(PlexLibraryItem(file_path="/stale.mkv", rating_key="5", section_key="1"))
    session.commit()

    count = rebuild_library_items(
        session, base_url=BASE_URL, token=TOKEN, list_sections=_sections(), list_items=_items({})
    )

    assert count == 0
    assert _rows(session) == {}


def test_rebuild_all_sections_reporting_zero_items_is_a_truthful_empty_commit(
    session: Session,
) -> None:
    """Every section succeeds but genuinely has no items -- also a truthful
    empty rebuild, distinct from an every-section-failure."""
    session.add(PlexLibraryItem(file_path="/stale.mkv", rating_key="5", section_key="1"))
    session.commit()

    sections = _sections(LibrarySection(key="1", title="Movies", type="movie"))

    count = rebuild_library_items(
        session, base_url=BASE_URL, token=TOKEN, list_sections=sections, list_items=_items({})
    )

    assert count == 0
    assert _rows(session) == {}


# --------------------------------------------------------------------------- #
# resolve_rating_key
# --------------------------------------------------------------------------- #
def _add_movie(session: Session, *, file_path: str, title: str) -> None:
    """A Radarr-bridged tracked file + its Movie library node."""
    session.add(TrackedMediaFile(file_path=file_path, instance_id=1, radarr_movie_id=42))
    session.add(
        LibraryNode(
            instance_id=1,
            kind=LibraryNodeKind.MOVIE,
            node_key=make_node_key(LibraryNodeKind.MOVIE, movie_id=42),
            radarr_movie_id=42,
            title=title,
        )
    )
    session.commit()


def _add_episode(
    session: Session, *, file_path: str, series_title: str, season: int, episode: int
) -> None:
    """A Sonarr-bridged tracked file + its Series and Episode library nodes."""
    session.add(TrackedMediaFile(file_path=file_path, instance_id=1, sonarr_episode_id=77))
    session.add(
        LibraryNode(
            instance_id=1,
            kind=LibraryNodeKind.SERIES,
            node_key=make_node_key(LibraryNodeKind.SERIES, series_id=7),
            sonarr_series_id=7,
            title=series_title,
        )
    )
    session.add(
        LibraryNode(
            instance_id=1,
            kind=LibraryNodeKind.EPISODE,
            node_key=make_node_key(LibraryNodeKind.EPISODE, episode_id=77),
            sonarr_series_id=7,
            season_number=season,
            episode_number=episode,
            sonarr_episode_id=77,
            title="The Episode Title",
        )
    )
    session.commit()


def test_resolve_returns_rating_key_from_the_mapping_table_without_querying(
    session: Session,
) -> None:
    session.add(PlexLibraryItem(file_path="/movies/a.mkv", rating_key="101", section_key="1"))
    session.commit()
    record: list[str] = []

    rating_key = resolve_rating_key(
        session,
        "/movies/a.mkv",
        base_url=BASE_URL,
        token=TOKEN,
        search=_search(SectionItemsResult(ok=True), record=record),
    )

    assert rating_key == "101"
    assert record == []  # a table hit never falls back to a live query


def test_resolve_falls_back_to_a_single_scoped_query_for_a_movie(session: Session) -> None:
    _add_movie(session, file_path="/movies/dune.mkv", title="Dune")
    record: list[str] = []
    search = _search(
        SectionItemsResult(
            ok=True, items=(PlexMediaItem(rating_key="900", type="movie", title="Dune"),)
        ),
        record=record,
    )

    rating_key = resolve_rating_key(
        session, "/movies/dune.mkv", base_url=BASE_URL, token=TOKEN, search=search
    )

    assert rating_key == "900"
    assert record == ["Dune"]  # scoped by the movie title, exactly once


def test_resolve_falls_back_and_matches_episode_by_series_title_season_and_episode(
    session: Session,
) -> None:
    _add_episode(
        session, file_path="/tv/got/s03e09.mkv", series_title="Game of Thrones", season=3, episode=9
    )
    record: list[str] = []
    search = _search(
        SectionItemsResult(
            ok=True,
            items=(
                PlexMediaItem(rating_key="1", type="episode", season_number=3, episode_number=8),
                PlexMediaItem(rating_key="2", type="episode", season_number=3, episode_number=9),
            ),
        ),
        record=record,
    )

    rating_key = resolve_rating_key(
        session, "/tv/got/s03e09.mkv", base_url=BASE_URL, token=TOKEN, search=search
    )

    assert rating_key == "2"
    assert record == ["Game of Thrones"]  # scoped by the *series* title


def test_resolve_gives_up_when_file_is_not_tracked(session: Session) -> None:
    record: list[str] = []

    rating_key = resolve_rating_key(
        session,
        "/unknown.mkv",
        base_url=BASE_URL,
        token=TOKEN,
        search=_search(SectionItemsResult(ok=True), record=record),
    )

    assert rating_key is None
    assert record == []  # nothing to scope a query by -> no query issued


def test_resolve_gives_up_when_search_reports_failure(session: Session) -> None:
    _add_movie(session, file_path="/movies/dune.mkv", title="Dune")

    rating_key = resolve_rating_key(
        session,
        "/movies/dune.mkv",
        base_url=BASE_URL,
        token=TOKEN,
        search=_search(SectionItemsResult(ok=False, error="offline")),
    )

    assert rating_key is None


def test_resolve_gives_up_when_no_candidate_matches(session: Session) -> None:
    _add_episode(
        session, file_path="/tv/got/s03e09.mkv", series_title="Game of Thrones", season=3, episode=9
    )
    search = _search(
        SectionItemsResult(
            ok=True,
            items=(
                PlexMediaItem(rating_key="1", type="episode", season_number=1, episode_number=1),
            ),
        )
    )

    rating_key = resolve_rating_key(
        session, "/tv/got/s03e09.mkv", base_url=BASE_URL, token=TOKEN, search=search
    )

    assert rating_key is None


def test_resolve_gives_up_silently_when_base_url_is_blank(session: Session) -> None:
    _add_movie(session, file_path="/movies/dune.mkv", title="Dune")
    record: list[str] = []

    rating_key = resolve_rating_key(
        session,
        "/movies/dune.mkv",
        base_url="",
        token=TOKEN,
        search=_search(SectionItemsResult(ok=True), record=record),
    )

    assert rating_key is None
    assert record == []


def test_resolve_swallows_an_unexpected_error_and_gives_up(session: Session) -> None:
    _add_movie(session, file_path="/movies/dune.mkv", title="Dune")

    rating_key = resolve_rating_key(
        session,
        "/movies/dune.mkv",
        base_url=BASE_URL,
        token=TOKEN,
        search=_search(SectionItemsResult(ok=True), raises=True),
    )

    assert rating_key is None  # a raising query must never propagate


def test_resolve_swallows_an_error_from_the_mapping_table_lookup_itself(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB error on the *cheap indexed lookup* (step 1, before any live query
    is even considered) must also give up silently, not propagate -- the whole
    path is soft-fail by design, not just the live-query fallback (code-review
    follow-up)."""
    _add_movie(session, file_path="/movies/dune.mkv", title="Dune")
    record: list[str] = []

    def raising_scalars(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(session, "scalars", raising_scalars)

    rating_key = resolve_rating_key(
        session,
        "/movies/dune.mkv",
        base_url=BASE_URL,
        token=TOKEN,
        search=_search(SectionItemsResult(ok=True), record=record),
    )

    assert rating_key is None
    assert record == []  # never even reached the live-query fallback
