"""Resolve Preferred Default Audio over Plex-reported streams (COL-240).

:mod:`collapsarr.downmix.default_audio` answers "which stream should carry
the Default Audio Track disposition" over a *locally ffprobe-probed* stream
list. This module answers the exact same question over a *Plex-reported*
stream list (:mod:`collapsarr.plex.streams`'s :class:`~collapsarr.plex.
streams.PlexAudioStream`, parsed from a ``GET /library/metadata/{ratingKey}``
response) instead -- the shape a later ticket's direct-Plex-API-write path
(COL-245) needs, as an alternative to the remux-based fix when Collapsarr can
reach the Plex server directly and doesn't need to touch the file at all.

The resolution order is identical, deliberately duplicated rather than
shared: :func:`~collapsarr.downmix.default_audio.resolve_default_audio_stream`
operates on :class:`~collapsarr.downmix.probe.AudioStreamInfo` (which carries
an ffprobe ``index`` used to break channel-count ties toward the
lowest-numbered stream); :class:`~collapsarr.plex.streams.PlexAudioStream`
carries no such index (COL-240's acceptance criteria list only
id/channels/language/title/extended_display_title/selected), so this module
instead breaks ties toward the stream that appears **first in the input
list** -- equivalent behaviour given "equivalent stream data" (both sources
report streams in the container's own part order).

1. **Exact match** -- a stream whose language equals the preference's
   language *and* whose channel count equals the preferred channel tier's
   channel count.
2. **Tier fallback within language** -- the preferred language is present but
   not at the preferred tier: fall back to the best-available stream
   (highest channel count, ties broken toward the earliest stream in the
   input) within that same language.
3. **Language fallback** -- the preferred language isn't present at all:
   fall back to the best-available stream across every language present.

A stream list with fewer than two entries has nothing to compare, so
:func:`resolve_default_audio_stream` returns ``None`` -- "nothing to
change" -- without evaluating the preference at all, mirroring
:func:`~collapsarr.downmix.default_audio.resolve_default_audio_stream`.

Pure function over plain data -- no I/O, no DB, not yet called from any Job
(COL-240's explicit scope: the direct-Plex-API-write path that will call this
is COL-245).
"""

from __future__ import annotations

from collections.abc import Sequence

from collapsarr.downmix.default_audio import DefaultAudioPreference
from collapsarr.downmix.targets import DownmixTarget
from collapsarr.plex.streams import PlexAudioStream

# Deliberately a private, module-local copy -- see
# `collapsarr.downmix.default_audio`'s own copy of this exact mapping for why
# each module keeps its own rather than sharing one.
_TARGET_CHANNELS: dict[DownmixTarget, int] = {
    DownmixTarget.STEREO: 2,
    DownmixTarget.TWO_POINT_ONE: 3,
    DownmixTarget.FIVE_POINT_ONE: 6,
}


def resolve_default_audio_stream(
    streams: Sequence[PlexAudioStream], preference: DefaultAudioPreference
) -> PlexAudioStream | None:
    """Return the Plex-reported stream that should carry Preferred Default Audio.

    See the module docstring for the full three-step resolution order.
    Returns ``None`` -- "nothing to change" -- when there are fewer than two
    streams to compare, regardless of what ``preference`` says.
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
    streams: Sequence[PlexAudioStream], preference: DefaultAudioPreference
) -> PlexAudioStream | None:
    """Return the stream matching the preference's exact ``(language, tier)``, if any."""
    target_channels = _TARGET_CHANNELS[preference.channel_tier]
    for stream in streams:
        if stream.language == preference.language and stream.channels == target_channels:
            return stream
    return None


def _best_available(streams: Sequence[PlexAudioStream]) -> PlexAudioStream:
    """Return the highest-channel-count stream, ties broken toward the earliest in the input."""
    return min(
        enumerate(streams), key=lambda pair: (-pair[1].channels, pair[0])
    )[1]
