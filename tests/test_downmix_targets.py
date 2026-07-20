"""Tests for qualifying downmix target detection (COL-16).

``detect_qualifying_targets`` is a pure function: build
:class:`~collapsarr.downmix.probe.AudioStreamInfo` fixtures directly (no
ffprobe/subprocess involved, unlike ``tests/test_downmix_probe.py``) and
assert against the returned ``(language, target)`` pairs.
"""

from __future__ import annotations

from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import (
    DownmixSettings,
    DownmixTarget,
    QualifyingTarget,
    detect_qualifying_targets,
)

ALL_TARGETS = frozenset(
    {DownmixTarget.STEREO, DownmixTarget.TWO_POINT_ONE, DownmixTarget.FIVE_POINT_ONE}
)


def _stream(
    *, index: int = 0, channels: int, language: str = "eng", codec: str = "flac"
) -> AudioStreamInfo:
    return AudioStreamInfo(
        index=index,
        codec=codec,
        channels=channels,
        channel_layout=f"{channels}ch",
        language=language,
    )


def test_7_1_source_with_all_targets_enabled_stacks_all_three() -> None:
    """A 7.1 source with everything enabled yields Stereo + 2.1 + 5.1, stacked."""
    streams = [_stream(channels=8)]
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)

    result = detect_qualifying_targets(streams, settings)

    assert result == [
        QualifyingTarget(language="eng", target=DownmixTarget.STEREO),
        QualifyingTarget(language="eng", target=DownmixTarget.TWO_POINT_ONE),
        QualifyingTarget(language="eng", target=DownmixTarget.FIVE_POINT_ONE),
    ]


def test_7_1_source_with_partial_target_enablement() -> None:
    """Only enabled targets are considered, even if others would otherwise qualify."""
    streams = [_stream(channels=8)]
    settings = DownmixSettings(
        enabled_targets=frozenset({DownmixTarget.STEREO, DownmixTarget.FIVE_POINT_ONE})
    )

    result = detect_qualifying_targets(streams, settings)

    assert result == [
        QualifyingTarget(language="eng", target=DownmixTarget.STEREO),
        QualifyingTarget(language="eng", target=DownmixTarget.FIVE_POINT_ONE),
    ]


def test_5_1_source_never_gets_a_redundant_5_1_target() -> None:
    """5.1 source can gain Stereo/2.1 but never another 5.1 (no upmix, no dup tier)."""
    streams = [_stream(channels=6)]
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)

    result = detect_qualifying_targets(streams, settings)

    assert result == [
        QualifyingTarget(language="eng", target=DownmixTarget.STEREO),
        QualifyingTarget(language="eng", target=DownmixTarget.TWO_POINT_ONE),
    ]


def test_stereo_only_source_qualifies_for_nothing() -> None:
    """A stereo-only source never qualifies for anything (2.1/5.1 would be upmixes)."""
    streams = [_stream(channels=2)]
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)

    assert detect_qualifying_targets(streams, settings) == []


def test_target_already_present_for_language_is_skipped() -> None:
    """A language that already has a stream at a target's channel count skips that target."""
    streams = [_stream(channels=8), _stream(channels=2)]  # both English
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)

    result = detect_qualifying_targets(streams, settings)

    # Stereo (2ch) already present for eng -> skipped; 2.1/5.1 still qualify.
    assert result == [
        QualifyingTarget(language="eng", target=DownmixTarget.TWO_POINT_ONE),
        QualifyingTarget(language="eng", target=DownmixTarget.FIVE_POINT_ONE),
    ]


def test_multi_language_tracks_are_evaluated_independently() -> None:
    """Each language's qualifying targets are computed from its own streams only."""
    streams = [
        _stream(index=0, channels=8, language="eng"),
        _stream(index=1, channels=6, language="fre"),
        _stream(index=2, channels=2, language="jpn"),
    ]
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)

    result = detect_qualifying_targets(streams, settings)

    assert result == [
        QualifyingTarget(language="eng", target=DownmixTarget.STEREO),
        QualifyingTarget(language="eng", target=DownmixTarget.TWO_POINT_ONE),
        QualifyingTarget(language="eng", target=DownmixTarget.FIVE_POINT_ONE),
        QualifyingTarget(language="fre", target=DownmixTarget.STEREO),
        QualifyingTarget(language="fre", target=DownmixTarget.TWO_POINT_ONE),
        # jpn (stereo-only) qualifies for nothing.
    ]


def test_language_allow_list_narrows_evaluated_languages() -> None:
    """Languages outside the allow-list are omitted from the result, not errored."""
    streams = [
        _stream(index=0, channels=8, language="eng"),
        _stream(index=1, channels=8, language="fre"),
    ]
    settings = DownmixSettings(
        enabled_targets=ALL_TARGETS, language_allow_list=frozenset({"eng"})
    )

    result = detect_qualifying_targets(streams, settings)

    assert all(pair.language == "eng" for pair in result)
    assert result == [
        QualifyingTarget(language="eng", target=DownmixTarget.STEREO),
        QualifyingTarget(language="eng", target=DownmixTarget.TWO_POINT_ONE),
        QualifyingTarget(language="eng", target=DownmixTarget.FIVE_POINT_ONE),
    ]


def test_empty_allow_list_excludes_every_language() -> None:
    """An explicitly empty allow-list evaluates no languages at all."""
    streams = [_stream(channels=8)]
    settings = DownmixSettings(enabled_targets=ALL_TARGETS, language_allow_list=frozenset())

    assert detect_qualifying_targets(streams, settings) == []


def test_no_streams_returns_empty_list() -> None:
    """A file with no audio streams qualifies for nothing."""
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)

    assert detect_qualifying_targets([], settings) == []


def test_default_settings_enable_only_stereo() -> None:
    """Default DownmixSettings matches the product default: Stereo only, all languages."""
    streams = [_stream(channels=8)]

    result = detect_qualifying_targets(streams, DownmixSettings())

    assert result == [QualifyingTarget(language="eng", target=DownmixTarget.STEREO)]


def test_unknown_language_tag_is_evaluated_like_any_other_language() -> None:
    """The "unknown" language bucket from probe.py is treated as an ordinary language."""
    streams = [_stream(channels=8, language="unknown")]
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)

    result = detect_qualifying_targets(streams, settings)

    assert result == [
        QualifyingTarget(language="unknown", target=DownmixTarget.STEREO),
        QualifyingTarget(language="unknown", target=DownmixTarget.TWO_POINT_ONE),
        QualifyingTarget(language="unknown", target=DownmixTarget.FIVE_POINT_ONE),
    ]
