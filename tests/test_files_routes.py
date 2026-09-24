"""Contract tests for the single-file lookup REST endpoints (COL-203, COL-205).

Covers request/response shape and the API-key-required behaviour (COL-26) for
``GET /api/files/{file_id}``. Unlike ``GET /api/wanted`` (COL-28), this
endpoint resolves a tracked file by id regardless of whether it currently
has any missing targets -- the key regression this ticket exists to fix
(previously the file detail page scanned ``GET /api/wanted``, which excludes
fully-processed files entirely). Tracked media is seeded through the real
:mod:`collapsarr.media.service` upsert path (via the shared ``session``
fixture, which shares the SQLite file the ``client`` app reads), so the
endpoint exercises genuine data rather than a stub.

Also covers ``GET /api/files/{file_id}/poster`` (COL-205, real Plex
resolution since COL-212) and ``GET /api/files/{file_id}/poster/image``
(COL-212): a placeholder for an unconfigured/unmapped file, real
``status="available"``/``poster_url`` metadata plus a streamed image once
Plex is configured and the file is mapped, and ``404`` only for a
``file_id`` that doesn't resolve at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.arr.catalog import CatalogEpisode, CatalogSeries, SonarrCatalog
from collapsarr.arr.models import ArrInstance, InstanceType
from collapsarr.config import Settings
from collapsarr.downmix.probe import AudioStreamInfo, FfprobeError
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.health import DiskUsage
from collapsarr.library.service import sync_library
from collapsarr.main import create_app
from collapsarr.media.models import TrackedMediaFile
from collapsarr.media.service import upsert_tracked_media
from collapsarr.plex.client import ItemMetadataResult, PosterImageResult
from collapsarr.plex.models import PLEX_CONNECTION_ID, PlexConnection, PlexLibraryItem
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


# --- GET /api/files/{file_id}/audio-streams (COL-204) ------------------------


def test_audio_streams_endpoint_returns_the_current_live_probe(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response reuses `probe_audio_streams` -- the same probe the
    downmix/default-audio pipelines call -- and reports each stream's
    language, channel count, and whether it currently carries the Default
    Audio Track disposition."""
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    probed = [
        AudioStreamInfo(
            index=0,
            codec="eac3",
            channels=6,
            channel_layout="5.1",
            language="eng",
            is_default=True,
        ),
        AudioStreamInfo(
            index=1,
            codec="aac",
            channels=2,
            channel_layout="stereo",
            language="jpn",
            is_default=False,
        ),
    ]

    def fake_probe(file_path: str, **_kwargs: object) -> list[AudioStreamInfo]:
        assert file_path == media.file_path
        return probed

    monkeypatch.setattr("collapsarr.media.routes.probe_audio_streams", fake_probe)

    response = client.get(f"/api/files/{media.id}/audio-streams", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["probeable"] is True
    assert body["error"] is None
    assert body["streams"] == [
        {
            "index": 0,
            "codec": "eac3",
            "channels": 6,
            "channel_layout": "5.1",
            "language": "eng",
            "is_default": True,
        },
        {
            "index": 1,
            "codec": "aac",
            "channels": 2,
            "channel_layout": "stereo",
            "language": "jpn",
            "is_default": False,
        },
    ]


def test_audio_streams_endpoint_degrades_gracefully_for_an_unprobeable_file(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A known file that can't currently be probed (e.g. missing on disk)
    still returns 200 with `probeable=False`, rather than a 500 -- the file
    detail page must not crash on this."""
    media = upsert_tracked_media(
        session,
        file_path="/media/missing.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    def fake_probe(file_path: str, **_kwargs: object) -> list[AudioStreamInfo]:
        raise FfprobeError(f"no such file: {file_path!r}")

    monkeypatch.setattr("collapsarr.media.routes.probe_audio_streams", fake_probe)

    response = client.get(f"/api/files/{media.id}/audio-streams", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["probeable"] is False
    assert body["streams"] == []
    assert "no such file" in body["error"]


# --- Plex-aware `is_default` override (COL-256) ------------------------------
#
# On a Plex-connected deployment, the "Set Default Audio Track" job writes
# directly to Plex's API and never touches the local file (see
# `collapsarr.plex.default_audio_write`) -- so raw local ffprobe disposition
# flags can be stale, or even show *every* stream as default at once, while
# Plex itself has exactly one stream selected. These cases assert the
# endpoint sources `is_default` from Plex's own live-reported selected
# stream (matched to the local probe list by position, not id) whenever Plex
# is configured and resolvable, with a soft-fail back to raw local
# disposition otherwise.


def _plex_stream_entry(*, stream_id: str, selected: bool) -> dict[str, object]:
    return {
        "id": stream_id,
        "streamType": 2,
        "channels": 6,
        "languageCode": "eng",
        "selected": selected,
    }


def _plex_metadata_payload(streams: list[dict[str, object]]) -> dict[str, object]:
    return {
        "MediaContainer": {
            "Metadata": [{"Media": [{"id": 1, "Part": [{"id": 1, "Stream": streams}]}]}]
        }
    }


def _seed_plex_connected_file(
    session: Session, *, file_path: str = "/media/movie.mkv", rating_key: str = "555"
) -> TrackedMediaFile:
    session.add(
        PlexConnection(id=PLEX_CONNECTION_ID, base_url="http://plex.local:32400", token="tok")
    )
    media = upsert_tracked_media(
        session,
        file_path=file_path,
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )
    session.add(PlexLibraryItem(file_path=file_path, rating_key=rating_key, section_key="1"))
    session.commit()
    return media


def test_audio_streams_endpoint_overrides_stale_local_defaults_from_plexs_selected_stream(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this ticket fixes: the local file has *both* streams
    flagged default (stale disposition), but Plex reports exactly one
    selected stream (the second one) -- the response must show only that
    one as default, matching Plex's live state."""
    media = _seed_plex_connected_file(session)

    probed = [
        AudioStreamInfo(
            index=0,
            codec="eac3",
            channels=6,
            channel_layout="5.1",
            language="eng",
            is_default=True,
        ),
        AudioStreamInfo(
            index=1,
            codec="aac",
            channels=2,
            channel_layout="stereo",
            language="jpn",
            is_default=True,
        ),
    ]
    monkeypatch.setattr(
        "collapsarr.media.routes.probe_audio_streams", lambda *_a, **_k: probed
    )

    payload = _plex_metadata_payload(
        [
            _plex_stream_entry(stream_id="101", selected=False),
            _plex_stream_entry(stream_id="102", selected=True),
        ]
    )
    monkeypatch.setattr(
        "collapsarr.media.routes.get_item_metadata",
        lambda *_a, **_k: ItemMetadataResult(ok=True, payload=payload),
    )

    response = client.get(f"/api/files/{media.id}/audio-streams", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    defaults = [stream["is_default"] for stream in body["streams"]]
    assert defaults == [False, True]


def test_audio_streams_endpoint_shows_no_default_when_plex_reports_none_selected(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plex resolves and reports *no* stream currently selected -- trusted as
    a genuine answer (every stream non-default), not a resolution failure,
    even though the local probe still has one flagged default."""
    media = _seed_plex_connected_file(session)

    probed = [
        AudioStreamInfo(
            index=0, codec="eac3", channels=6, channel_layout="5.1", language="eng", is_default=True
        ),
    ]
    monkeypatch.setattr(
        "collapsarr.media.routes.probe_audio_streams", lambda *_a, **_k: probed
    )

    payload = _plex_metadata_payload([_plex_stream_entry(stream_id="101", selected=False)])
    monkeypatch.setattr(
        "collapsarr.media.routes.get_item_metadata",
        lambda *_a, **_k: ItemMetadataResult(ok=True, payload=payload),
    )

    response = client.get(f"/api/files/{media.id}/audio-streams", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert [stream["is_default"] for stream in body["streams"]] == [False]


def test_audio_streams_endpoint_falls_back_to_local_when_plex_is_not_configured(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default (unconfigured) `PlexConnection` row -- untouched local
    disposition flags pass straight through, exactly the pre-COL-256
    behavior."""
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )
    probed = [
        AudioStreamInfo(
            index=0, codec="eac3", channels=6, channel_layout="5.1", language="eng", is_default=True
        ),
    ]
    monkeypatch.setattr(
        "collapsarr.media.routes.probe_audio_streams", lambda *_a, **_k: probed
    )

    def _unexpected_call(*_a: object, **_k: object) -> ItemMetadataResult:
        raise AssertionError("get_item_metadata must not be called when Plex isn't configured")

    monkeypatch.setattr("collapsarr.media.routes.get_item_metadata", _unexpected_call)

    response = client.get(f"/api/files/{media.id}/audio-streams", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    assert response.json()["streams"][0]["is_default"] is True


def test_audio_streams_endpoint_falls_back_to_local_when_the_rating_key_never_resolves(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plex is configured but the file has no mapping-table row and no
    bridged Library node for the live fallback -- soft-fails to local
    disposition, same as the poster endpoints' own miss case."""
    session.add(
        PlexConnection(id=PLEX_CONNECTION_ID, base_url="http://plex.local:32400", token="tok")
    )
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )
    probed = [
        AudioStreamInfo(
            index=0, codec="eac3", channels=6, channel_layout="5.1", language="eng", is_default=True
        ),
    ]
    monkeypatch.setattr(
        "collapsarr.media.routes.probe_audio_streams", lambda *_a, **_k: probed
    )

    response = client.get(f"/api/files/{media.id}/audio-streams", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    assert response.json()["streams"][0]["is_default"] is True


def test_audio_streams_endpoint_falls_back_to_local_when_the_plex_metadata_fetch_fails(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient Plex fetch failure soft-fails to local disposition rather
    than surfacing an error or a broken table."""
    media = _seed_plex_connected_file(session)
    probed = [
        AudioStreamInfo(
            index=0, codec="eac3", channels=6, channel_layout="5.1", language="eng", is_default=True
        ),
    ]
    monkeypatch.setattr(
        "collapsarr.media.routes.probe_audio_streams", lambda *_a, **_k: probed
    )
    monkeypatch.setattr(
        "collapsarr.media.routes.get_item_metadata",
        lambda *_a, **_k: ItemMetadataResult(ok=False, error="HTTP 500: boom"),
    )

    response = client.get(f"/api/files/{media.id}/audio-streams", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    assert response.json()["streams"][0]["is_default"] is True


def test_audio_streams_endpoint_falls_back_to_local_on_a_stream_count_mismatch(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plex reports a different number of audio streams than the local probe
    -- position can no longer be trusted to identify the same physical
    stream on both sides, so this soft-fails to local disposition rather
    than risking a wrong override."""
    media = _seed_plex_connected_file(session)
    probed = [
        AudioStreamInfo(
            index=0,
            codec="eac3",
            channels=6,
            channel_layout="5.1",
            language="eng",
            is_default=True,
        ),
        AudioStreamInfo(
            index=1,
            codec="aac",
            channels=2,
            channel_layout="stereo",
            language="jpn",
            is_default=False,
        ),
    ]
    monkeypatch.setattr(
        "collapsarr.media.routes.probe_audio_streams", lambda *_a, **_k: probed
    )
    # Plex reports only one audio stream -- a count mismatch against the two
    # locally-probed streams above.
    payload = _plex_metadata_payload([_plex_stream_entry(stream_id="101", selected=True)])
    monkeypatch.setattr(
        "collapsarr.media.routes.get_item_metadata",
        lambda *_a, **_k: ItemMetadataResult(ok=True, payload=payload),
    )

    response = client.get(f"/api/files/{media.id}/audio-streams", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    defaults = [stream["is_default"] for stream in response.json()["streams"]]
    assert defaults == [True, False]


def test_audio_streams_endpoint_returns_not_found_for_an_id_that_never_existed(
    client: TestClient, session: Session
) -> None:
    response = client.get("/api/files/999999/audio-streams", headers=_auth_headers(client))

    assert response.status_code == 404, response.text


def test_audio_streams_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    response = client.get(f"/api/files/{media.id}/audio-streams")
    assert response.status_code == 401


# --- GET /api/files/{id}/poster (COL-205, real Plex resolution since COL-212) -----------------


def test_poster_endpoint_returns_the_placeholder_state_when_plex_is_not_configured(
    client: TestClient, session: Session
) -> None:
    """Plex isn't configured (the default, blank `PlexConnection` row) -- the
    endpoint falls back to the placeholder state for a file that exists,
    never a poster URL and never an error."""
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    response = client.get(f"/api/files/{media.id}/poster", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"file_id": media.id, "status": "placeholder", "poster_url": None}


def test_poster_endpoint_returns_the_placeholder_state_on_a_mapping_and_live_fallback_miss(
    client: TestClient, session: Session
) -> None:
    """Plex is configured but the file has no mapping-table row and the live
    fallback also can't find it (no bridged Library node here) -- still a
    placeholder, not an error."""
    session.add(
        PlexConnection(id=PLEX_CONNECTION_ID, base_url="http://plex.local:32400", token="tok")
    )
    session.commit()
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    response = client.get(f"/api/files/{media.id}/poster", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"file_id": media.id, "status": "placeholder", "poster_url": None}


def test_poster_endpoint_returns_available_with_a_poster_url_on_a_mapping_hit(
    client: TestClient, session: Session
) -> None:
    """Plex is configured and the file is mapped -- the endpoint returns
    `status="available"` with a same-origin `poster_url` pointing at the
    image-streaming endpoint, never the Plex server's own URL or token."""
    session.add(
        PlexConnection(id=PLEX_CONNECTION_ID, base_url="http://plex.local:32400", token="tok")
    )
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )
    session.add(PlexLibraryItem(file_path=media.file_path, rating_key="555", section_key="1"))
    session.commit()

    response = client.get(f"/api/files/{media.id}/poster", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "file_id": media.id,
        "status": "available",
        "poster_url": f"/api/files/{media.id}/poster/image",
    }
    # The Plex token/base URL never appear anywhere in the response body.
    assert "tok" not in response.text
    assert "plex.local" not in response.text


def test_poster_endpoint_returns_not_found_for_an_id_that_never_existed(
    client: TestClient, session: Session
) -> None:
    response = client.get("/api/files/999999/poster", headers=_auth_headers(client))

    assert response.status_code == 404, response.text


def test_poster_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    response = client.get(f"/api/files/{media.id}/poster")
    assert response.status_code == 401


# --- GET /api/files/{id}/poster/image (COL-212) -------------------------------------


def test_poster_image_endpoint_streams_the_resolved_posters_bytes(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary happy path: a mapped file with Plex configured resolves
    its `ratingKey` and streams the upstream image's bytes and content type
    straight through, with the token never appearing in the response."""
    session.add(
        PlexConnection(id=PLEX_CONNECTION_ID, base_url="http://plex.local:32400", token="tok")
    )
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )
    session.add(PlexLibraryItem(file_path=media.file_path, rating_key="555", section_key="1"))
    session.commit()

    image_bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes"

    def fake_fetch_poster_image(base_url: str, token: str, rating_key: str) -> PosterImageResult:
        assert base_url == "http://plex.local:32400"
        assert token == "tok"
        assert rating_key == "555"
        return PosterImageResult(ok=True, content=image_bytes, content_type="image/jpeg")

    monkeypatch.setattr("collapsarr.media.routes.fetch_poster_image", fake_fetch_poster_image)

    response = client.get(f"/api/files/{media.id}/poster/image", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    assert response.content == image_bytes
    assert response.headers["content-type"] == "image/jpeg"
    # The Plex token never appears in the streamed response.
    assert b"tok" not in response.content


def test_poster_image_endpoint_returns_not_found_when_plex_is_not_configured(
    client: TestClient, session: Session
) -> None:
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    response = client.get(f"/api/files/{media.id}/poster/image", headers=_auth_headers(client))

    assert response.status_code == 404, response.text


def test_poster_image_endpoint_returns_not_found_when_the_upstream_fetch_fails(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.add(
        PlexConnection(id=PLEX_CONNECTION_ID, base_url="http://plex.local:32400", token="tok")
    )
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )
    session.add(PlexLibraryItem(file_path=media.file_path, rating_key="555", section_key="1"))
    session.commit()

    def fake_fetch_poster_image(base_url: str, token: str, rating_key: str) -> PosterImageResult:
        return PosterImageResult(ok=False, error="HTTP 404: not found")

    monkeypatch.setattr("collapsarr.media.routes.fetch_poster_image", fake_fetch_poster_image)

    response = client.get(f"/api/files/{media.id}/poster/image", headers=_auth_headers(client))

    assert response.status_code == 404, response.text


def test_poster_image_endpoint_returns_not_found_for_an_id_that_never_existed(
    client: TestClient, session: Session
) -> None:
    response = client.get("/api/files/999999/poster/image", headers=_auth_headers(client))

    assert response.status_code == 404, response.text


def test_poster_image_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    response = client.get(f"/api/files/{media.id}/poster/image")
    assert response.status_code == 401


# --- poster_url carries the reverse-proxy url_base prefix (COL-117/COL-212) ---
#
# `poster_url` is sent straight to the browser as an `<img src>` -- bypassing
# the frontend's own `prefixPath()`/`apiFetch` layer entirely -- so unlike
# every other JSON field in this module it must already carry the configured
# `url_base` prefix itself under a reverse-proxy subpath deployment. This
# needs its own app instance (the shared `client` fixture has no `url_base`
# configured), mirroring `tests/test_url_base.py`'s `_make_client` helper.


class _FakeDiskUsage(NamedTuple):
    total: int
    used: int
    free: int


def _ample_free_space(_path: str) -> DiskUsage:
    """Deterministic disk-usage stand-in (see tests/conftest.py's fixture)."""
    return _FakeDiskUsage(total=1000, used=100, free=900)


def _offline_update_check_transport() -> httpx.MockTransport:
    """Deterministic, offline stand-in for the Update Check scheduler's fetch."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="offline in tests")

    return httpx.MockTransport(handler)


def _make_client_with_url_base(tmp_path: Path, *, url_base: str) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "collapsarr.db"),
        data_dir=str(tmp_path),
        url_base=url_base,
    )
    app = create_app(
        settings=settings,
        disk_usage=_ample_free_space,
        update_check_transport=_offline_update_check_transport(),
    )
    return TestClient(app)


def test_poster_endpoint_prefixes_poster_url_with_the_configured_url_base(tmp_path: Path) -> None:
    with _make_client_with_url_base(tmp_path, url_base="/collapsarr") as client:
        app = client.app
        assert isinstance(app, FastAPI)
        with app.state.session_factory() as session:
            session.add(
                PlexConnection(
                    id=PLEX_CONNECTION_ID, base_url="http://plex.local:32400", token="tok"
                )
            )
            media = upsert_tracked_media(
                session,
                file_path="/media/movie.mkv",
                streams=[_stream(channels=8)],
                settings=DownmixSettings(enabled_targets=ALL_TARGETS),
            )
            session.add(
                PlexLibraryItem(file_path=media.file_path, rating_key="555", section_key="1")
            )
            session.commit()
            media_id = media.id

        response = client.get(
            f"/collapsarr/api/files/{media_id}/poster", headers=_auth_headers(client)
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "available"
        assert body["poster_url"] == f"/collapsarr/api/files/{media_id}/poster/image"
