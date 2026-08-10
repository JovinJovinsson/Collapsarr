"""Resolve which audio stream should carry the Default Audio Track disposition (COL-151).

:mod:`collapsarr.downmix.probe` (COL-15/COL-150) turns a file into a flat list
of :class:`~collapsarr.downmix.probe.AudioStreamInfo`, each now also
reporting whether it *currently* carries the container's Default Audio Track
disposition flag (``is_default``). This module is the next step: given that
stream list plus the user's **Preferred Default Audio** setting -- a
``(language, channel tier)`` pair, the channel tier reusing
:class:`~collapsarr.downmix.targets.DownmixTarget` rather than inventing a
parallel enum, same reuse :class:`~collapsarr.downmix.targets.DownmixSettings`
already makes -- decide which single stream *should* carry that disposition.

This is intentionally a pure function over plain data, mirroring
:func:`~collapsarr.downmix.targets.detect_qualifying_targets`: no I/O, no DB,
no awareness of the persisted :class:`~collapsarr.settings.models.GlobalSettings`
row or its ``auto_set_default_audio`` toggle -- gating on that toggle, and
actually applying the disposition to a file, are later slices' concern
(COL-152 automatic, COL-153 manual/bulk). This module only answers "which
stream, if any, should be default".

Resolution order, strict and first-match-wins:

1. **Exact match** -- a stream whose language equals the preference's
   language *and* whose channel count equals the preferred channel tier's
   channel count.
2. **Tier fallback within language** -- the preferred language is present on
   the file but not at the preferred tier: fall back to the
   *best-available* stream (highest channel count, ties broken toward the
   lowest stream index -- the same tie-break
   :mod:`collapsarr.downmix.remux` already documents for picking a downmix
   source stream) within that same language.
3. **Language fallback** -- the preferred language isn't present on the file
   at all: fall back to the best-available stream across *every* language
   present. This is an expected, ordinary outcome (e.g. a file that simply
   doesn't carry the preferred language track), not an error condition.

A file with fewer than two audio streams (a single track, or none at all)
has nothing to compare, so :func:`resolve_default_audio_stream` returns
``None`` -- "nothing to change" -- without evaluating the preference at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixTarget

# Deliberately a private, module-local copy rather than importing
# `collapsarr.downmix.targets`'s own private mapping -- `collapsarr.downmix.remux`
# already keeps its own copy of this exact mapping for the same reason (each
# module's channel-count-per-tier is a fixed, tiny constant, not worth a
# shared-module dependency just to avoid three lines of duplication).
_TARGET_CHANNELS: dict[DownmixTarget, int] = {
    DownmixTarget.STEREO: 2,
    DownmixTarget.TWO_POINT_ONE: 3,
    DownmixTarget.FIVE_POINT_ONE: 6,
}


@dataclass(frozen=True, slots=True)
class DefaultAudioPreference:
    """The Preferred Default Audio setting: a ``(language, channel tier)`` pair.

    Plain input data for :func:`resolve_default_audio_stream` -- not a
    persisted model. Adapting the persisted
    :class:`~collapsarr.settings.models.GlobalSettings` row's
    ``default_audio_language``/``default_audio_channel_tier`` columns into
    this shape (and deciding what to do when either is unset) is a caller
    concern, same as :func:`~collapsarr.settings.service.as_downmix_settings`
    adapts settings into :class:`~collapsarr.downmix.targets.DownmixSettings`.
    """

    language: str
    channel_tier: DownmixTarget


def resolve_default_audio_stream(
    streams: Sequence[AudioStreamInfo], preference: DefaultAudioPreference
) -> AudioStreamInfo | None:
    """Return the stream that should carry the Default Audio Track disposition.

    See the module docstring for the full three-step resolution order.
    Returns ``None`` -- "nothing to change" -- when there are fewer than two
    streams to compare (a single-track file, or a file with no audio streams
    at all), regardless of what ``preference`` says.
    """
    if len(streams) < 2:
        return None

    exact_match = _find_exact_match(streams, preference)
    if exact_match is not None:
        return exact_match

    same_language = [stream for stream in streams if stream.language == preference.language]
    if same_language:
        return _best_available(same_language)

    return _best_available(streams)


def _find_exact_match(
    streams: Sequence[AudioStreamInfo], preference: DefaultAudioPreference
) -> AudioStreamInfo | None:
    """Return the stream matching the preference's exact ``(language, tier)``, if any."""
    target_channels = _TARGET_CHANNELS[preference.channel_tier]
    for stream in streams:
        if stream.language == preference.language and stream.channels == target_channels:
            return stream
    return None


def _best_available(streams: Sequence[AudioStreamInfo]) -> AudioStreamInfo:
    """Return the highest-channel-count stream, ties broken toward the lowest index."""
    return min(streams, key=lambda stream: (-stream.channels, stream.index))
