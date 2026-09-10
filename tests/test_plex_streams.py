"""Tests for parsing Plex metadata responses into audio streams (COL-240).

Pure-function tests: build ``GET /library/metadata/{ratingKey}`` response
payloads directly as plain dicts (no httpx/network involved), mirroring
``tests/test_downmix_probe.py``'s malformed-payload coverage but for
:func:`collapsarr.plex.streams.parse_audio_streams`.
"""

from __future__ import annotations

from collapsarr.plex.streams import PlexAudioStream, is_commentary_track, parse_audio_streams


def _metadata_payload(streams: list[object]) -> dict[str, object]:
    return {
        "MediaContainer": {
            "size": 1,
            "Metadata": [
                {
                    "ratingKey": "123",
                    "Media": [
                        {
                            "id": 1,
                            "Part": [
                                {
                                    "id": 1,
                                    "Stream": streams,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    }


def _audio_stream(
    *,
    stream_id: object = 1,
    channels: object = 6,
    language_code: object = "eng",
    title: object = None,
    extended_display_title: object = None,
    selected: object = False,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": stream_id,
        "streamType": 2,
        "channels": channels,
        "languageCode": language_code,
        "selected": selected,
    }
    if title is not None:
        entry["title"] = title
    if extended_display_title is not None:
        entry["extendedDisplayTitle"] = extended_display_title
    return entry


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_parses_audio_stream_fields() -> None:
    payload = _metadata_payload(
        [
            _audio_stream(
                stream_id=101,
                channels=6,
                language_code="eng",
                title="Commentary",
                extended_display_title="English (AC3 5.1)",
                selected=True,
            )
        ]
    )

    result = parse_audio_streams(payload)

    assert result == [
        PlexAudioStream(
            id="101",
            part_id="1",
            channels=6,
            language="eng",
            title="Commentary",
            extended_display_title="English (AC3 5.1)",
            selected=True,
        )
    ]


def test_stream_id_is_kept_as_string_when_already_a_string() -> None:
    payload = _metadata_payload([_audio_stream(stream_id="abc123")])

    result = parse_audio_streams(payload)

    assert result[0].id == "abc123"


# ---------------------------------------------------------------------------
# Part id (COL-252): the set-default-audio write targets the containing Part,
# not the item's ratingKey, so `part_id` must be captured off the payload's
# ``Media[].Part[].id`` the same way `id` is off `Stream.id`.
# ---------------------------------------------------------------------------


def test_part_id_is_captured_and_coerced_to_string() -> None:
    # _metadata_payload nests every stream under a Part whose ``id`` is the
    # int ``1``.
    payload = _metadata_payload([_audio_stream(stream_id=1)])

    result = parse_audio_streams(payload)

    assert result[0].part_id == "1"


def test_part_id_is_kept_as_string_when_already_a_string() -> None:
    payload: dict[str, object] = {
        "MediaContainer": {
            "Metadata": [
                {
                    "Media": [
                        {
                            "Part": [
                                {
                                    "id": "part-abc",
                                    "Stream": [_audio_stream(stream_id=1)],
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    }

    result = parse_audio_streams(payload)

    assert result[0].part_id == "part-abc"


def test_part_missing_id_skips_every_stream_in_that_part() -> None:
    payload: dict[str, object] = {
        "MediaContainer": {
            "Metadata": [
                {
                    "Media": [
                        {
                            "Part": [
                                {"Stream": [_audio_stream(stream_id=1, channels=2)]},
                                {"id": 20, "Stream": [_audio_stream(stream_id=2, channels=6)]},
                            ]
                        }
                    ]
                }
            ]
        }
    }

    result = parse_audio_streams(payload)

    assert [stream.id for stream in result] == ["2"]
    assert result[0].part_id == "20"


def test_missing_title_and_extended_display_title_are_none() -> None:
    payload = _metadata_payload([_audio_stream(stream_id=1)])

    result = parse_audio_streams(payload)

    assert result[0].title is None
    assert result[0].extended_display_title is None


# ---------------------------------------------------------------------------
# Filtering: only audio (streamType == 2) streams survive; video/subtitle skipped.
# ---------------------------------------------------------------------------


def test_non_audio_streams_are_skipped() -> None:
    video_stream: dict[str, object] = {"id": 1, "streamType": 1, "channels": 0}
    subtitle_stream: dict[str, object] = {"id": 2, "streamType": 3}
    audio_stream = _audio_stream(stream_id=3, channels=2)
    payload = _metadata_payload([video_stream, subtitle_stream, audio_stream])

    result = parse_audio_streams(payload)

    assert len(result) == 1
    assert result[0].id == "3"


def test_multiple_parts_are_flattened_in_order() -> None:
    payload = {
        "MediaContainer": {
            "Metadata": [
                {
                    "Media": [
                        {
                            "Part": [
                                {"id": 10, "Stream": [_audio_stream(stream_id=1, channels=2)]},
                                {"id": 20, "Stream": [_audio_stream(stream_id=2, channels=6)]},
                            ]
                        }
                    ]
                }
            ]
        }
    }

    result = parse_audio_streams(payload)

    assert [stream.id for stream in result] == ["1", "2"]
    assert [stream.part_id for stream in result] == ["10", "20"]


# ---------------------------------------------------------------------------
# Language normalization: mirrors collapsarr.downmix.probe's ffprobe handling.
# ---------------------------------------------------------------------------


def test_missing_language_code_normalizes_to_unknown() -> None:
    payload = _metadata_payload([{"id": 1, "streamType": 2, "channels": 2}])

    result = parse_audio_streams(payload)

    assert result[0].language == "unknown"


def test_null_language_code_normalizes_to_unknown() -> None:
    payload = _metadata_payload([_audio_stream(stream_id=1, language_code=None)])

    result = parse_audio_streams(payload)

    assert result[0].language == "unknown"


def test_undetermined_language_code_normalizes_to_unknown() -> None:
    payload = _metadata_payload([_audio_stream(stream_id=1, language_code="und")])

    result = parse_audio_streams(payload)

    assert result[0].language == "unknown"


def test_language_code_is_lowercased() -> None:
    payload = _metadata_payload([_audio_stream(stream_id=1, language_code="ENG")])

    result = parse_audio_streams(payload)

    assert result[0].language == "eng"


# ---------------------------------------------------------------------------
# Malformed / unexpected payload shapes: skip rather than raise.
# ---------------------------------------------------------------------------


def test_non_dict_payload_returns_empty_list() -> None:
    assert parse_audio_streams(["not", "a", "dict"]) == []
    assert parse_audio_streams(None) == []


def test_missing_media_container_returns_empty_list() -> None:
    assert parse_audio_streams({"foo": "bar"}) == []


def test_missing_metadata_list_returns_empty_list() -> None:
    assert parse_audio_streams({"MediaContainer": {}}) == []


def test_stream_missing_id_is_skipped() -> None:
    payload = _metadata_payload(
        [
            {"streamType": 2, "channels": 2},
            _audio_stream(stream_id=2, channels=6),
        ]
    )

    result = parse_audio_streams(payload)

    assert len(result) == 1
    assert result[0].id == "2"


def test_stream_missing_channels_is_skipped() -> None:
    payload = _metadata_payload(
        [
            {"id": 1, "streamType": 2},
            _audio_stream(stream_id=2, channels=6),
        ]
    )

    result = parse_audio_streams(payload)

    assert len(result) == 1
    assert result[0].id == "2"


def test_non_dict_entries_in_stream_list_are_skipped() -> None:
    payload = _metadata_payload(["not-a-dict", _audio_stream(stream_id=1, channels=2)])

    result = parse_audio_streams(payload)

    assert len(result) == 1


def test_empty_stream_list_returns_empty_list() -> None:
    payload = _metadata_payload([])

    assert parse_audio_streams(payload) == []


# ---------------------------------------------------------------------------
# is_commentary_track: title-only detection, no native Plex disposition (COL-246).
# ---------------------------------------------------------------------------


def _stream(
    *, title: str | None = None, extended_display_title: str | None = None
) -> PlexAudioStream:
    return PlexAudioStream(
        id="1",
        part_id="1",
        channels=2,
        language="eng",
        title=title,
        extended_display_title=extended_display_title,
        selected=False,
    )


def test_is_commentary_track_true_for_title_containing_comment() -> None:
    assert is_commentary_track(_stream(title="Director's Commentary")) is True


def test_is_commentary_track_true_for_extended_display_title_containing_comment() -> None:
    assert (
        is_commentary_track(
            _stream(title=None, extended_display_title="English (Commentary, AC3 2.0)")
        )
        is True
    )


def test_is_commentary_track_match_is_case_insensitive() -> None:
    assert is_commentary_track(_stream(title="COMMENTARY")) is True
    assert is_commentary_track(_stream(title="commentary")) is True


def test_is_commentary_track_false_when_neither_field_mentions_comment() -> None:
    assert (
        is_commentary_track(
            _stream(title="English", extended_display_title="English (AC3 5.1)")
        )
        is False
    )


def test_is_commentary_track_false_when_both_fields_are_none() -> None:
    assert is_commentary_track(_stream(title=None, extended_display_title=None)) is False
