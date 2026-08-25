"""Tests for the Plex Sync scheduler orchestration (COL-210).

Follows the sibling schedulers' idiom (``test_update_check_scheduler`` /
``test_health_scheduler``): an injectable clock (``now``) so ``last_sync_at``
is asserted without real wall-clock time, ``run_once`` driven directly as the
seam (in-memory ``list_sections``/``list_items`` fakes, no real Plex), and
**real threading only for the single lifecycle test** (the ticket's "real
thread start/stop is covered by exactly one lifecycle test").
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.media.models import TrackedMediaFile
from collapsarr.migrations import upgrade_to_head
from collapsarr.plex.client import LibrarySection, PlexMediaItem, SectionItemsResult, SectionsResult
from collapsarr.plex.models import PlexLibraryItem
from collapsarr.plex.scheduler import INTERVAL_SECONDS, PlexSyncScheduler
from collapsarr.plex.service import get_plex_connection

_FIXED_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
_BASE_URL = "http://plex.local:32400"


@pytest.fixture
def session_factory(settings: Settings) -> Iterator[sessionmaker[Session]]:
    """A schema-initialised session factory over the isolated ``settings`` DB."""
    upgrade_to_head(settings)
    engine = create_engine_from_settings(settings)
    yield create_session_factory(engine)
    engine.dispose()


def _configure_connection(session_factory: sessionmaker[Session], *, base_url: str) -> None:
    with session_factory() as session:
        connection = get_plex_connection(session)
        connection.base_url = base_url
        connection.token = "plex-token"
        session.commit()


def _sections(*sections: LibrarySection) -> Callable[..., SectionsResult]:
    def fn(base_url: str, token: str, *, transport: object | None = None) -> SectionsResult:
        return SectionsResult(ok=True, sections=tuple(sections))

    return fn


def _items(by_section: dict[str, Sequence[PlexMediaItem]]) -> Callable[..., SectionItemsResult]:
    def fn(
        base_url: str,
        token: str,
        section_key: str,
        *,
        item_type: str | None = None,
        transport: object | None = None,
    ) -> SectionItemsResult:
        return SectionItemsResult(ok=True, items=tuple(by_section.get(section_key, ())))

    return fn


def _make_scheduler(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    now: datetime = _FIXED_NOW,
    list_sections: Callable[..., SectionsResult] | None = None,
    list_items: Callable[..., SectionItemsResult] | None = None,
    transport: httpx.BaseTransport | None = None,
    interval_seconds: float = INTERVAL_SECONDS,
) -> PlexSyncScheduler:
    return PlexSyncScheduler(
        settings,
        session_factory,
        now=lambda: now,
        list_sections=list_sections or _sections(),
        list_items=list_items or _items({}),
        transport=transport,
        interval_seconds=interval_seconds,
    )


def _rows(session_factory: sessionmaker[Session]) -> dict[str, tuple[str, str]]:
    with session_factory() as session:
        return {
            row.file_path: (row.rating_key, row.section_key)
            for row in session.scalars(select(PlexLibraryItem))
        }


# --------------------------------------------------------------------------- #
# Fixed weekly cadence
# --------------------------------------------------------------------------- #
def test_interval_is_weekly() -> None:
    assert INTERVAL_SECONDS == 7 * 24 * 60 * 60


# --------------------------------------------------------------------------- #
# run_once rebuilds the mapping table from the persisted connection
# --------------------------------------------------------------------------- #
def test_run_once_rebuilds_the_mapping_table(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _configure_connection(session_factory, base_url=_BASE_URL)
    scheduler = _make_scheduler(
        settings,
        session_factory,
        list_sections=_sections(LibrarySection(key="1", title="Movies", type="movie")),
        list_items=_items(
            {"1": [PlexMediaItem(rating_key="101", file_paths=("/movies/a.mkv",), type="movie")]}
        ),
    )

    count = scheduler.run_once()

    assert count == 1
    assert _rows(session_factory) == {"/movies/a.mkv": ("101", "1")}


def test_run_once_noops_when_connection_is_unconfigured_but_still_stamps_last_sync(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    # No connection configured -> blank base_url -> rebuild is a no-op.
    scheduler = _make_scheduler(settings, session_factory)

    count = scheduler.run_once()

    assert count == 0
    assert _rows(session_factory) == {}
    assert scheduler.last_sync_at is not None  # a no-op tick still stamps last-run


def test_last_sync_at_is_stamped_with_the_injected_clock(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory, now=_FIXED_NOW)
    assert scheduler.last_sync_at is None

    scheduler.run_once()

    assert scheduler.last_sync_at == _FIXED_NOW


# --------------------------------------------------------------------------- #
# request_sync is non-blocking: it only arms the loop's wake/run flags
# --------------------------------------------------------------------------- #
def test_request_sync_arms_the_wake_and_sync_flags(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    scheduler = _make_scheduler(settings, session_factory)

    scheduler.request_sync()  # must not block, must not run a tick itself

    assert scheduler._sync_requested.is_set()
    assert scheduler._wake.is_set()
    assert scheduler.last_sync_at is None  # nothing ran on the calling thread


# --------------------------------------------------------------------------- #
# Background loop: real threading, daemon thread, immediate tick, request_sync
# wakes it for an off-cycle tick, clean stop. (The single lifecycle test.)
# --------------------------------------------------------------------------- #
def test_start_ticks_immediately_request_sync_retriggers_then_stops_cleanly(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _configure_connection(session_factory, base_url=_BASE_URL)
    ticks: list[float] = []

    def counting_sections(
        base_url: str, token: str, *, transport: object | None = None
    ) -> SectionsResult:
        ticks.append(time.monotonic())
        return SectionsResult(ok=True, sections=())

    scheduler = _make_scheduler(settings, session_factory, list_sections=counting_sections)

    scheduler.start()
    try:
        assert scheduler._thread is not None
        assert scheduler._thread.daemon is True
        assert scheduler._thread.name == "collapsarr-plex-sync-scheduler"
        _wait_until(lambda: len(ticks) >= 1, timeout=5.0)  # immediate first tick

        scheduler.request_sync()  # off-cycle: wakes the ~weekly sleep for another tick
        _wait_until(lambda: len(ticks) >= 2, timeout=5.0)
    finally:
        scheduler.stop()

    assert scheduler._thread is None  # joined + cleared on stop
    assert scheduler.last_sync_at is not None


# --------------------------------------------------------------------------- #
# run_once also refreshes the Default Audio Track display snapshot (COL-248)
# --------------------------------------------------------------------------- #
def test_run_once_refreshes_default_audio_track_snapshot_from_plex(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    """A default changed directly in Plex's UI (not via a Collapsarr Job) is
    reflected in the tracked file's snapshot columns after one sync run --
    driven end to end through ``run_once`` with an ``httpx.MockTransport``
    standing in for the real Plex metadata GET (COL-248 AC)."""
    _configure_connection(session_factory, base_url=_BASE_URL)
    file_path = "/movies/a.mkv"
    rating_key = "101"
    with session_factory() as session:
        session.add(
            TrackedMediaFile(
                file_path=file_path,
                current_default_language="fra",
                current_default_channel_layout="stereo",
            )
        )
        session.commit()

    def metadata_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [
                        {
                            "ratingKey": rating_key,
                            "Media": [
                                {
                                    "Part": [
                                        {
                                            "Stream": [
                                                {
                                                    "id": "1",
                                                    "streamType": 2,
                                                    "channels": 2,
                                                    "languageCode": "fra",
                                                    "selected": False,
                                                },
                                                {
                                                    "id": "2",
                                                    "streamType": 2,
                                                    "channels": 6,
                                                    "languageCode": "eng",
                                                    "selected": True,
                                                },
                                            ]
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                }
            },
        )

    scheduler = _make_scheduler(
        settings,
        session_factory,
        list_sections=_sections(LibrarySection(key="1", title="Movies", type="movie")),
        list_items=_items(
            {"1": [PlexMediaItem(rating_key=rating_key, file_paths=(file_path,), type="movie")]}
        ),
        transport=httpx.MockTransport(metadata_handler),
    )

    scheduler.run_once()

    with session_factory() as session:
        media = session.scalars(
            select(TrackedMediaFile).where(TrackedMediaFile.file_path == file_path)
        ).one()
        assert media.current_default_language == "eng"
        assert media.current_default_channel_layout == "5.1"


def _wait_until(predicate: Callable[[], bool], *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")
