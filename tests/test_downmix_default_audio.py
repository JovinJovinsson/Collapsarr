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
    resolve_default_audio_output_index,
    resolve_default_audio_stream,
)
from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixTarget, QualifyingTarget


def _stream(
    *,
    index: int,
    channels: int,
    language: str = "eng",
    codec: str = "flac",
    is_default: bool = False,
    is_commentary: bool = False,
    title: str | None = None,
) -> AudioStreamInfo:
    return AudioStreamInfo(
        index=index,
        codec=codec,
        channels=channels,
        channel_layout=f"{channels}ch",
        language=language,
        is_default=is_default,
        is_commentary=is_commentary,
        title=title,
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


# ---------------------------------------------------------------------------
# Commentary exclusion (COL-250): ignore_commentary_tracks removes commentary
# streams from the candidate pool at every resolution step, unless every
# stream on the file is commentary.
# ---------------------------------------------------------------------------


def test_ignore_commentary_tracks_off_lets_a_commentary_track_win_exact_match() -> None:
    """Default (``False``) preserves pre-COL-250 behavior: no filtering at all."""
    streams = [
        _stream(index=0, channels=6, language="eng"),
        _stream(index=1, channels=2, language="eng", is_commentary=True),
    ]
    preference = DefaultAudioPreference(
        language="eng", channel_tier=DownmixTarget.STEREO, ignore_commentary_tracks=False
    )

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[1]


def test_ignore_commentary_tracks_excludes_exact_match_commentary_stream() -> None:
    """A commentary track that would otherwise be the exact match is skipped."""
    streams = [
        _stream(index=0, channels=6, language="eng"),
        _stream(index=1, channels=2, language="eng", is_commentary=True),
    ]
    preference = DefaultAudioPreference(
        language="eng", channel_tier=DownmixTarget.STEREO, ignore_commentary_tracks=True
    )

    # No non-commentary eng stereo track exists -> falls through to tier
    # fallback within eng, landing on the non-commentary 6ch stream.
    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[0]


def test_ignore_commentary_tracks_detects_via_title_not_just_disposition_flag() -> None:
    """Title-based detection (`is_commentary_track`'s other signal) is honored too."""
    streams = [
        _stream(index=0, channels=6, language="eng"),
        _stream(
            index=1, channels=6, language="eng", title="Director's Commentary", is_commentary=False
        ),
    ]
    preference = DefaultAudioPreference(
        language="eng", channel_tier=DownmixTarget.STEREO, ignore_commentary_tracks=True
    )

    # Both are 6ch eng, tied on channel count -- the commentary-titled stream
    # must be excluded from the tie-break entirely, leaving only streams[0].
    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[0]


def test_ignore_commentary_tracks_excludes_from_language_fallback() -> None:
    streams = [
        _stream(index=0, channels=2, language="jpn"),
        _stream(index=1, channels=6, language="fre", is_commentary=True),
    ]
    preference = DefaultAudioPreference(
        language="eng", channel_tier=DownmixTarget.STEREO, ignore_commentary_tracks=True
    )

    # eng isn't present at all; fre's only track is commentary and excluded,
    # so the best-available *non-commentary* stream overall wins: jpn.
    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[0]


def test_ignore_commentary_tracks_falls_back_to_commentary_when_all_streams_are_commentary() -> (
    None
):
    """No non-commentary alternative anywhere -> filtering is skipped entirely."""
    streams = [
        _stream(index=0, channels=6, language="eng", is_commentary=True),
        _stream(index=1, channels=2, language="eng", is_commentary=True),
    ]
    preference = DefaultAudioPreference(
        language="eng", channel_tier=DownmixTarget.STEREO, ignore_commentary_tracks=True
    )

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[1]


def test_ignore_commentary_tracks_true_is_a_noop_when_no_stream_is_commentary() -> None:
    streams = [
        _stream(index=0, channels=6, language="eng"),
        _stream(index=1, channels=2, language="eng"),
    ]
    preference = DefaultAudioPreference(
        language="eng", channel_tier=DownmixTarget.STEREO, ignore_commentary_tracks=True
    )

    result = resolve_default_audio_stream(streams, preference)

    assert result == streams[1]


# ---------------------------------------------------------------------------
# resolve_default_audio_output_index: resolve over the *final* output layout
# (existing streams + newly-encoded targets), returning the output audio-relative
# index that should carry the disposition, or None when nothing needs to change.
# ---------------------------------------------------------------------------


def test_output_index_selects_a_newly_encoded_track_when_it_is_the_exact_match() -> None:
    """A downmix that adds the preferred tier makes the *new* track the winner."""
    streams = [_stream(index=0, channels=6, language="eng", is_default=True)]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.STEREO)]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    # Final layout: [orig eng 6ch @ a:0, new eng stereo @ a:1] -> new track wins.
    assert resolve_default_audio_output_index(streams, targets, preference) == 1


def test_output_index_selects_an_existing_stream_that_is_not_yet_default() -> None:
    streams = [
        _stream(index=0, channels=2, language="eng", is_default=False),
        _stream(index=1, channels=6, language="eng", is_default=True),
    ]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.TWO_POINT_ONE)]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    # Exact match is the eng 2ch stream at output audio index 0 (currently not
    # default) -- the 6ch stream currently holds it, so a change is needed.
    assert resolve_default_audio_output_index(streams, targets, preference) == 0


def test_output_index_is_none_when_winner_already_default_and_nothing_else_is() -> None:
    streams = [
        _stream(index=0, channels=6, language="eng", is_default=True),
        _stream(index=1, channels=2, language="eng", is_default=False),
    ]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.TWO_POINT_ONE)]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE)

    # Winner is the eng 6ch stream, which already carries the disposition, and
    # no other stream does -> nothing to change.
    assert resolve_default_audio_output_index(streams, targets, preference) is None


def test_output_index_set_when_winner_already_default_but_another_stream_also_is() -> None:
    streams = [
        _stream(index=0, channels=6, language="eng", is_default=True),
        _stream(index=1, channels=6, language="jpn", is_default=True),
    ]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.STEREO)]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE)

    # Winner (eng 6ch @ a:0) is already default, but jpn 6ch @ a:1 wrongly
    # carries it too -> a change is still needed to clear that one.
    assert resolve_default_audio_output_index(streams, targets, preference) == 0


def test_output_index_maps_stacked_new_tracks_in_target_order() -> None:
    streams = [_stream(index=0, channels=6, language="fre", is_default=True)]
    targets = [
        QualifyingTarget(language="fre", target=DownmixTarget.STEREO),
        QualifyingTarget(language="fre", target=DownmixTarget.TWO_POINT_ONE),
    ]
    preference = DefaultAudioPreference(language="fre", channel_tier=DownmixTarget.TWO_POINT_ONE)

    # Final layout: [fre 6ch @ a:0, new fre stereo @ a:1, new fre 2.1 @ a:2].
    # Exact match for the 2.1 tier is the second new track, at output index 2.
    assert resolve_default_audio_output_index(streams, targets, preference) == 2


def test_output_index_is_none_when_final_layout_has_fewer_than_two_streams() -> None:
    streams = [_stream(index=0, channels=6, language="eng", is_default=False)]
    preference = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO)

    # No targets, single existing stream -> nothing to compare, nothing to change.
    assert resolve_default_audio_output_index(streams, [], preference) is None


def test_output_index_excludes_an_existing_commentary_stream_from_the_winner(
) -> None:
    """A currently-default commentary stream is displaced by a non-commentary one."""
    streams = [
        _stream(index=0, channels=6, language="eng", is_default=True, is_commentary=True),
        _stream(index=1, channels=2, language="eng", is_default=False),
    ]
    preference = DefaultAudioPreference(
        language="eng", channel_tier=DownmixTarget.STEREO, ignore_commentary_tracks=True
    )

    # Commentary stream is excluded from the candidate pool entirely, so the
    # eng stereo stream wins the exact match even though it isn't yet default.
    assert resolve_default_audio_output_index(streams, [], preference) == 1
