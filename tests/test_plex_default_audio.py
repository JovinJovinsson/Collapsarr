"""Tests for Plex-based Preferred Default Audio resolution (COL-240).

``resolve_default_audio_stream`` is a pure function: build
:class:`~collapsarr.plex.streams.PlexAudioStream` fixtures directly (no
httpx/network involved), mirroring
``tests/test_downmix_default_audio.py``'s pattern -- the same preference
logic, over Plex-reported streams instead of ffprobe-probed ones, asserting
it picks the *equivalent* stream the local resolver would given equivalent
stream data.
"""

from __future__ import annotations

from collapsarr.downmix.default_audio import DefaultAudioPreference
from collapsarr.downmix.targets import DownmixTarget
from collapsarr.plex.default_audio import resolve_default_audio_stream
from collapsarr.plex.streams import PlexAudioStream


def _stream(
    *,
    stream_id: str,
    channels: int,
    language: str = "eng",
    selected: bool = False,
) -> PlexAudioStream:
    return PlexAudioStream(
        id=stream_id,
        channels=channels,
        language=language,
        title=None,
        extended_display_title=None,
        selected=selected,
    )


# ---------------------------------------------------------------------------
# Nothing-to-compare: single track / no streams.
# ---------------------------------------------------------------------------


def test_no_streams_resolves_to_nothing_to_change() -> None:
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    assert resolve_default_audio_stream([], preference) is None


def test_single_track_resolves_to_nothing_to_change() -> None:
    streams = [_stream(stream_id="1", channels=6, language="jpn")]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    assert resolve_default_audio_stream(streams, preference) is None


# ---------------------------------------------------------------------------
# Step 1: exact (language, tier) match.
# ---------------------------------------------------------------------------


def test_exact_language_and_tier_match_is_picked() -> None:
    streams = [
        _stream(stream_id="1", channels=6, language="eng"),
        _stream(stream_id="2", channels=2, language="eng"),
        _stream(stream_id="3", channels=6, language="jpn"),
    ]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[1]


def test_exact_match_prefers_5_1_tier_when_requested() -> None:
    streams = [
        _stream(stream_id="1", channels=2, language="eng"),
        _stream(stream_id="2", channels=6, language="eng"),
    ]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[1]


def test_exact_match_requires_both_language_and_tier() -> None:
    """A stream matching the tier but not the language is not an exact match."""
    streams = [
        _stream(stream_id="1", channels=2, language="jpn"),
        _stream(stream_id="2", channels=6, language="eng"),
    ]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    # No exact match for eng+stereo -> falls back to best-available within eng.
    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[1]


# ---------------------------------------------------------------------------
# Step 2: tier not present for the matched language -> best-available within it.
# ---------------------------------------------------------------------------


def test_tier_absent_for_language_falls_back_to_best_available_in_language() -> None:
    streams = [
        _stream(stream_id="1", channels=2, language="eng"),
        _stream(stream_id="2", channels=6, language="eng"),
        _stream(stream_id="3", channels=8, language="jpn"),
    ]
    # eng has no 2.1 (3ch) track -> fall back to eng's best available (6ch).
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.TWO_POINT_ONE)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[1]


def test_best_available_within_language_ties_broken_toward_earliest_in_input() -> None:
    streams = [
        _stream(stream_id="1", channels=6, language="eng"),
        _stream(stream_id="2", channels=6, language="eng"),
    ]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[0]


# ---------------------------------------------------------------------------
# Step 3: preferred language absent from the file entirely -> best-available overall.
# ---------------------------------------------------------------------------


def test_preferred_language_absent_falls_back_to_best_available_overall() -> None:
    """Expected, non-error outcome when the preferred language isn't on the file."""
    streams = [
        _stream(stream_id="1", channels=2, language="jpn"),
        _stream(stream_id="2", channels=6, language="fre"),
    ]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[1]


def test_preferred_language_absent_ties_broken_toward_earliest_in_input() -> None:
    streams = [
        _stream(stream_id="1", channels=6, language="jpn"),
        _stream(stream_id="2", channels=6, language="fre"),
    ]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[0]


# ---------------------------------------------------------------------------
# The "unknown" language bucket is treated like any other language.
# ---------------------------------------------------------------------------


def test_unknown_language_preference_is_treated_like_any_other_language() -> None:
    streams = [
        _stream(stream_id="1", channels=2, language="unknown"),
        _stream(stream_id="2", channels=6, language="eng"),
    ]
    preference = DefaultAudioPreference(language="unknown", channel_tier=DownmixTarget.STEREO)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[0]


# ---------------------------------------------------------------------------
# Parity with the local ffprobe-based resolver over equivalent stream data.
# ---------------------------------------------------------------------------


def test_matches_local_resolver_outcome_given_equivalent_stream_data() -> None:
    """Same (language, channels) data via both resolvers picks the equivalent stream."""
    from collapsarr.downmix.default_audio import (
        resolve_default_audio_stream as resolve_local,
    )
    from collapsarr.downmix.probe import AudioStreamInfo

    local_streams = [
        AudioStreamInfo(index=0, codec="flac", channels=2, channel_layout="2ch", language="jpn"),
        AudioStreamInfo(index=1, codec="flac", channels=6, channel_layout="6ch", language="eng"),
        AudioStreamInfo(index=2, codec="flac", channels=2, channel_layout="2ch", language="eng"),
    ]
    plex_streams = [
        _stream(stream_id="1", channels=2, language="jpn"),
        _stream(stream_id="2", channels=6, language="eng"),
        _stream(stream_id="3", channels=2, language="eng"),
    ]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    local_result = resolve_local(local_streams, preference)
    plex_result = resolve_default_audio_stream(plex_streams, preference)

    assert local_result is not None and plex_result is not None
    assert local_result.channels == plex_result.channels
    assert local_result.language == plex_result.language
    assert plex_result is plex_streams[2]


def test_matches_local_resolver_tie_break_given_equivalent_stream_data() -> None:
    """Parity through the *tie-break* path: no exact match, two same-language streams
    tied on channel count, so both resolvers must fall back to
    ``_best_available``/``_best_available`` and land on the equivalent (earliest-
    in-list / lowest-index) stream -- not just whichever happens to satisfy the
    exact-match short-circuit, which is all the sibling parity test above
    exercises.
    """
    from collapsarr.downmix.default_audio import (
        resolve_default_audio_stream as resolve_local,
    )
    from collapsarr.downmix.probe import AudioStreamInfo

    # No eng stream at the preferred stereo (2ch) tier -> falls back to
    # best-available within eng, where the two eng streams are tied at 6ch.
    local_streams = [
        AudioStreamInfo(index=0, codec="flac", channels=6, channel_layout="6ch", language="eng"),
        AudioStreamInfo(index=1, codec="flac", channels=6, channel_layout="6ch", language="eng"),
        AudioStreamInfo(index=2, codec="flac", channels=2, channel_layout="2ch", language="jpn"),
    ]
    plex_streams = [
        _stream(stream_id="1", channels=6, language="eng"),
        _stream(stream_id="2", channels=6, language="eng"),
        _stream(stream_id="3", channels=2, language="jpn"),
    ]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    local_result = resolve_local(local_streams, preference)
    plex_result = resolve_default_audio_stream(plex_streams, preference)

    assert local_result is not None and plex_result is not None
    # Both resolvers break the 6ch/6ch tie toward the earliest entry: the
    # local resolver via `stream.index`, the Plex resolver via input position.
    assert local_result is local_streams[0]
    assert plex_result is plex_streams[0]
    assert local_result.channels == plex_result.channels
    assert local_result.language == plex_result.language
