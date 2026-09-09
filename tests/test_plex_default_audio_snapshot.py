"""Tests for the Plex Sync Default Audio Track display snapshot refresh (COL-248).

Every case drives :func:`~collapsarr.plex.default_audio_snapshot.
refresh_default_audio_snapshots` with an ``httpx.MockTransport`` -- no live
network call is made, mirroring ``tests/test_plex_default_audio_write.py``'s
pattern -- asserting the resulting
:class:`~collapsarr.media.models.TrackedMediaFile` snapshot columns.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.orm import Session

from collapsarr.media.models import TrackedMediaFile
from collapsarr.plex.default_audio_snapshot import refresh_default_audio_snapshots
from collapsarr.plex.models import PlexLibraryItem

BASE_URL = "http://plex.local:32400"
TOKEN = "plex-token"
FILE_PATH = "/media/movie.mkv"
RATING_KEY = "12345"


def _tracked(session: Session, *, file_path: str = FILE_PATH, **kwargs: object) -> TrackedMediaFile:
    media = TrackedMediaFile(file_path=file_path, **kwargs)
    session.add(media)
    session.commit()
    return media


def _mapped(session: Session, *, file_path: str = FILE_PATH, rating_key: str = RATING_KEY) -> None:
    session.add(PlexLibraryItem(file_path=file_path, rating_key=rating_key, section_key="1"))
    session.commit()


def _stream_entry(
    *, stream_id: str, channels: int = 6, language_code: str = "eng", selected: bool = False
) -> dict[str, object]:
    return {
        "id": stream_id,
        "streamType": 2,
        "channels": channels,
        "languageCode": language_code,
        "selected": selected,
    }


def _metadata_payload(streams: list[dict[str, object]]) -> dict[str, object]:
    return {
        "MediaContainer": {
            "Metadata": [
                {
                    "ratingKey": RATING_KEY,
                    "Media": [{"id": 1, "Part": [{"id": 1, "Stream": streams}]}],
                }
            ]
        }
    }


def _transport_for(payloads: dict[str, object]) -> httpx.MockTransport:
    """A transport returning ``payloads[ratingKey]`` for every metadata GET."""

    def handler(request: httpx.Request) -> httpx.Response:
        rating_key = request.url.path.rsplit("/", 1)[-1]
        payload = payloads.get(rating_key)
        if payload is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Core: a tracked, Plex-resolved file picks up Plex's currently-selected stream.
# ---------------------------------------------------------------------------


def test_refreshes_snapshot_from_plexs_currently_selected_stream(session: Session) -> None:
    _tracked(session, current_default_language=None, current_default_channel_layout=None)
    _mapped(session)
    payload = _metadata_payload(
        [
            _stream_entry(stream_id="1", channels=2, language_code="fra", selected=False),
            _stream_entry(stream_id="2", channels=6, language_code="eng", selected=True),
        ]
    )
    transport = _transport_for({RATING_KEY: payload})

    updated = refresh_default_audio_snapshots(
        session, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert updated == 1
    media = session.query(TrackedMediaFile).filter_by(file_path=FILE_PATH).one()
    assert media.current_default_language == "eng"
    assert media.current_default_channel_layout == "5.1"


def test_drift_in_plex_is_reflected_after_a_sync_run(session: Session) -> None:
    """A default changed directly in Plex's UI (not via a Collapsarr Job)
    shows the updated value after the next Plex Sync run (COL-248 AC #2)."""
    _tracked(session, current_default_language="fra", current_default_channel_layout="stereo")
    _mapped(session)
    # Plex now reports the eng/5.1 stream as selected -- the drifted state.
    payload = _metadata_payload(
        [
            _stream_entry(stream_id="1", channels=2, language_code="fra", selected=False),
            _stream_entry(stream_id="2", channels=6, language_code="eng", selected=True),
        ]
    )
    transport = _transport_for({RATING_KEY: payload})

    refresh_default_audio_snapshots(session, base_url=BASE_URL, token=TOKEN, transport=transport)

    media = session.query(TrackedMediaFile).filter_by(file_path=FILE_PATH).one()
    assert media.current_default_language == "eng"
    assert media.current_default_channel_layout == "5.1"


# ---------------------------------------------------------------------------
# Scope: only tracked AND Plex-resolved files are touched.
# ---------------------------------------------------------------------------


def test_untracked_file_is_never_touched_even_if_plex_resolved(session: Session) -> None:
    _mapped(session)  # a PlexLibraryItem row, but no TrackedMediaFile for it
    transport = _transport_for({RATING_KEY: _metadata_payload([])})

    updated = refresh_default_audio_snapshots(
        session, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert updated == 0


def test_tracked_but_not_plex_resolved_file_is_left_untouched(session: Session) -> None:
    _tracked(session, current_default_language="fra", current_default_channel_layout="stereo")
    # No PlexLibraryItem row for FILE_PATH -- not Plex-resolved.
    transport = _transport_for({})

    updated = refresh_default_audio_snapshots(
        session, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert updated == 0
    media = session.query(TrackedMediaFile).filter_by(file_path=FILE_PATH).one()
    assert media.current_default_language == "fra"
    assert media.current_default_channel_layout == "stereo"


# ---------------------------------------------------------------------------
# Soft-fail: a per-file metadata fetch failure leaves that file's snapshot alone.
# ---------------------------------------------------------------------------


def test_metadata_fetch_failure_leaves_existing_snapshot_unchanged(session: Session) -> None:
    _tracked(session, current_default_language="fra", current_default_channel_layout="stereo")
    _mapped(session)
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))

    updated = refresh_default_audio_snapshots(
        session, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert updated == 0
    media = session.query(TrackedMediaFile).filter_by(file_path=FILE_PATH).one()
    assert media.current_default_language == "fra"
    assert media.current_default_channel_layout == "stereo"


def test_one_files_fetch_failure_does_not_block_another_files_refresh(session: Session) -> None:
    _tracked(session, file_path="/media/a.mkv")
    _tracked(session, file_path="/media/b.mkv")
    _mapped(session, file_path="/media/a.mkv", rating_key="1")
    _mapped(session, file_path="/media/b.mkv", rating_key="2")
    payload_b = _metadata_payload([_stream_entry(stream_id="9", channels=2, selected=True)])

    def handler(request: httpx.Request) -> httpx.Response:
        rating_key = request.url.path.rsplit("/", 1)[-1]
        if rating_key == "1":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=payload_b)

    transport = httpx.MockTransport(handler)

    updated = refresh_default_audio_snapshots(
        session, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert updated == 1
    media_a = session.query(TrackedMediaFile).filter_by(file_path="/media/a.mkv").one()
    media_b = session.query(TrackedMediaFile).filter_by(file_path="/media/b.mkv").one()
    assert media_a.current_default_language is None  # untouched, never probed before
    assert media_b.current_default_language == "eng"
    assert media_b.current_default_channel_layout == "stereo"


# ---------------------------------------------------------------------------
# No selected stream -> "unknown" (None/None), same as an un-flagged local probe.
# ---------------------------------------------------------------------------


def test_no_selected_stream_resets_snapshot_to_unknown(session: Session) -> None:
    _tracked(session, current_default_language="eng", current_default_channel_layout="5.1")
    _mapped(session)
    payload = _metadata_payload(
        [_stream_entry(stream_id="1", channels=6, language_code="eng", selected=False)]
    )
    transport = _transport_for({RATING_KEY: payload})

    updated = refresh_default_audio_snapshots(
        session, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert updated == 1
    media = session.query(TrackedMediaFile).filter_by(file_path=FILE_PATH).one()
    assert media.current_default_language is None
    assert media.current_default_channel_layout is None


# ---------------------------------------------------------------------------
# Blank base_url -> no-op.
# ---------------------------------------------------------------------------


def test_blank_base_url_is_a_noop(session: Session) -> None:
    _tracked(session, current_default_language="fra", current_default_channel_layout="stereo")
    _mapped(session)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))

    updated = refresh_default_audio_snapshots(
        session, base_url="", token=TOKEN, transport=transport
    )

    assert updated == 0
    media = session.query(TrackedMediaFile).filter_by(file_path=FILE_PATH).one()
    assert media.current_default_language == "fra"


# ---------------------------------------------------------------------------
# Channel-layout naming: named tiers plus the "<channels>ch" fallback.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channels", "expected_layout"),
    [(2, "stereo"), (3, "2.1"), (6, "5.1"), (8, "8ch"), (1, "1ch")],
)
def test_channel_layout_naming(session: Session, channels: int, expected_layout: str) -> None:
    _tracked(session)
    _mapped(session)
    payload = _metadata_payload([_stream_entry(stream_id="1", channels=channels, selected=True)])
    transport = _transport_for({RATING_KEY: payload})

    refresh_default_audio_snapshots(session, base_url=BASE_URL, token=TOKEN, transport=transport)

    media = session.query(TrackedMediaFile).filter_by(file_path=FILE_PATH).one()
    assert media.current_default_channel_layout == expected_layout
