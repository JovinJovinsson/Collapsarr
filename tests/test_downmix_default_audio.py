"""Tests for Default Audio Track stream resolution (COL-151).

``resolve_default_audio_stream`` is a pure function: build
:class:`~collapsarr.downmix.probe.AudioStreamInfo` fixtures directly (no
ffprobe/subprocess involved, unlike ``tests/test_downmix_probe.py``) and
assert against the returned stream -- mirroring
``tests/test_downmix_targets.py``'s shape.
"""

from __future__ import annotations

from collapsarr.downmix.default_audio import (
    DefaultAudioPreference,
    resolve_default_audio_stream,
)
from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixTarget


def _stream(
    *, index: int, channels: int, language: str = "eng", codec: str = "flac"
) -> AudioStreamInfo:
    return AudioStreamInfo(
        index=index,
        codec=codec,
        channels=channels,
        channel_layout=f"{channels}ch",
        language=language,
    )


# ---------------------------------------------------------------------------
# Nothing-to-compare: single track / no streams.
# ---------------------------------------------------------------------------


def test_no_streams_resolves_to_nothing_to_change() -> None:
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    assert resolve_default_audio_stream([], preference) is None


def test_single_track_resolves_to_nothing_to_change() -> None:
    """Even when the lone track doesn't match the preference at all."""
    streams = [_stream(index=0, channels=6, language="jpn")]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    assert resolve_default_audio_stream(streams, preference) is None


# ---------------------------------------------------------------------------
# Step 1: exact (language, tier) match.
# ---------------------------------------------------------------------------


def test_exact_language_and_tier_match_is_picked() -> None:
    streams = [
        _stream(index=0, channels=6, language="eng"),
        _stream(index=1, channels=2, language="eng"),
        _stream(index=2, channels=6, language="jpn"),
    ]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[1]


def test_exact_match_prefers_5_1_tier_when_requested() -> None:
    streams = [
        _stream(index=0, channels=2, language="eng"),
        _stream(index=1, channels=6, language="eng"),
    ]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[1]


def test_exact_match_requires_both_language_and_tier() -> None:
    """A stream matching the tier but not the language is not an exact match."""
    streams = [
        _stream(index=0, channels=2, language="jpn"),
        _stream(index=1, channels=6, language="eng"),
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
        _stream(index=0, channels=2, language="eng"),
        _stream(index=1, channels=6, language="eng"),
        _stream(index=2, channels=8, language="jpn"),
    ]
    # eng has no 2.1 (3ch) track -> fall back to eng's best available (6ch).
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.TWO_POINT_ONE)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[1]


def test_best_available_within_language_ties_broken_toward_lowest_index() -> None:
    streams = [
        _stream(index=0, channels=6, language="eng"),
        _stream(index=1, channels=6, language="eng"),
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
        _stream(index=0, channels=2, language="jpn"),
        _stream(index=1, channels=6, language="fre"),
    ]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[1]


def test_preferred_language_absent_ties_broken_toward_lowest_index() -> None:
    streams = [
        _stream(index=0, channels=6, language="jpn"),
        _stream(index=1, channels=6, language="fre"),
    ]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[0]


# ---------------------------------------------------------------------------
# The "unknown" language bucket is treated like any other language.
# ---------------------------------------------------------------------------


def test_unknown_language_preference_is_treated_like_any_other_language() -> None:
    streams = [
        _stream(index=0, channels=2, language="unknown"),
        _stream(index=1, channels=6, language="eng"),
    ]
    preference = DefaultAudioPreference(language="unknown", channel_tier=DownmixTarget.STEREO)

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[0]
