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
from collapsarr.downmix.targets import DownmixTarget, QualifyingTarget

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


def resolve_default_audio_output_index(
    streams: Sequence[AudioStreamInfo],
    qualifying_targets: Sequence[QualifyingTarget],
    preference: DefaultAudioPreference,
) -> int | None:
    """Resolve the disposition winner over a downmix job's *final* stream layout.

    Where :func:`resolve_default_audio_stream` answers "which existing stream
    should be default", this answers the question the automatic in-band fix
    (COL-152) actually needs: given a file's current audio streams *plus* the
    new tracks a downmix job is about to encode, which **output** audio-relative
    index should carry the Default Audio Track disposition -- or ``None`` when
    nothing needs to change.

    The final output audio layout mirrors
    :func:`~collapsarr.downmix.remux.build_remux_command`: the existing streams
    first, in their probed order (output audio indices ``0 .. len(streams)-1``),
    then one freshly-encoded track per entry in ``qualifying_targets``, in that
    order (indices ``len(streams) ..``). New tracks are modelled as ordinary,
    non-default :class:`~collapsarr.downmix.probe.AudioStreamInfo` (their
    language and channel count are the only fields resolution reads), so a job
    that adds the preferred tier can legitimately make one of its *new* tracks
    the winner. New tracks are given indices above every existing stream's, so
    the "ties broken toward the lowest index" rule keeps preferring an existing
    stream on a tie.

    Returns ``None`` -- meaning "emit no disposition flags", so the remux stays
    byte-for-byte what it would be without this feature -- in two cases:

    - :func:`resolve_default_audio_stream` finds no winner (a final layout with
      fewer than two streams); or
    - the resolved winner already carries the disposition **and** no other
      output stream wrongly carries it too (the acceptance criteria's
      "nothing to change" no-op). A newly-encoded track can never satisfy this,
      as it starts non-default, so any job whose winner is a new track always
      returns its index.
    """
    final_streams = _final_audio_layout(streams, qualifying_targets)
    winner = resolve_default_audio_stream(final_streams, preference)
    if winner is None:
        return None

    output_index = next(i for i, stream in enumerate(final_streams) if stream is winner)

    already_correct = winner.is_default and not any(
        stream.is_default for i, stream in enumerate(final_streams) if i != output_index
    )
    if already_correct:
        return None
    return output_index


def _final_audio_layout(
    streams: Sequence[AudioStreamInfo], qualifying_targets: Sequence[QualifyingTarget]
) -> list[AudioStreamInfo]:
    """Model the output audio layout of a downmix: existing streams then new tracks.

    New tracks carry only the fields resolution reads (``language``,
    ``channels``, ``is_default=False``); ``index`` is set above every existing
    stream's so tie-breaks keep favouring existing streams, and the unused
    ``codec``/``channel_layout`` are left as empty placeholders.
    """
    layout = list(streams)
    next_index = max((stream.index for stream in streams), default=-1) + 1
    for offset, target in enumerate(qualifying_targets):
        layout.append(
            AudioStreamInfo(
                index=next_index + offset,
                codec="",
                channels=_TARGET_CHANNELS[target.target],
                channel_layout="",
                language=target.language,
                is_default=False,
            )
        )
    return layout


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
