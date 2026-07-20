"""Detect qualifying downmix targets for a media file's audio streams (COL-16).

:mod:`collapsarr.downmix.probe` (COL-15) turns a file into a flat list of
:class:`~collapsarr.downmix.probe.AudioStreamInfo` — one entry per audio
stream, each carrying its own language and channel count. This module is the
next step in the pipeline: given that stream list plus the user's downmix
settings (which targets are enabled, and an optional language allow-list),
decide which ``(language, target)`` pairs actually qualify for a new
downmixed track.

Per the product design (``docs/plans/2026-07-20-collapsarr-v1-design.md``),
three fixed targets exist — Stereo (2.0), 2.1, and 5.1 — and a target
qualifies for a given language only when all of the following hold:

- the target is enabled in settings;
- that language doesn't already have a stream at the target's exact channel
  count (skip a target already present);
- the target has fewer channels than that language's highest existing
  channel count (never upmix, never add a redundant same-or-higher tier).

Multiple qualifying targets stack: a 7.1 source with every target enabled
qualifies for Stereo, 2.1, *and* 5.1, none replacing the original 7.1 track.

This is intentionally a pure function over plain data — no I/O, no DB. A
persisted, DB-backed ``Settings`` model is out of scope for this ticket;
:class:`DownmixSettings` is a settings-*shaped* plain dataclass that a real
settings model can be adapted into later.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from collapsarr.downmix.probe import AudioStreamInfo


class DownmixTarget(Enum):
    """A supported downmix target."""

    STEREO = "stereo"
    TWO_POINT_ONE = "2.1"
    FIVE_POINT_ONE = "5.1"


_TARGET_CHANNELS: dict[DownmixTarget, int] = {
    DownmixTarget.STEREO: 2,
    DownmixTarget.TWO_POINT_ONE: 3,
    DownmixTarget.FIVE_POINT_ONE: 6,
}

# Fixed ascending-channel-count order, used to make stacked output
# deterministic regardless of the iteration order of `enabled_targets`.
_TARGET_ORDER: tuple[DownmixTarget, ...] = (
    DownmixTarget.STEREO,
    DownmixTarget.TWO_POINT_ONE,
    DownmixTarget.FIVE_POINT_ONE,
)


@dataclass(frozen=True, slots=True)
class DownmixSettings:
    """Settings-shaped input for target detection.

    Not a persisted model — a plain, immutable stand-in for the real
    Settings page (``docs/plans/2026-07-20-collapsarr-v1-design.md``) that a
    DB-backed model can later be adapted into.

    ``enabled_targets`` defaults to Stereo only, matching the product
    default (2.1/5.1 are opt-in). ``language_allow_list`` of ``None`` (the
    default) evaluates every language present on the file; a non-``None``
    set restricts evaluation to just those languages — languages outside it
    are silently omitted from the result, never errored.
    """

    enabled_targets: frozenset[DownmixTarget] = frozenset({DownmixTarget.STEREO})
    language_allow_list: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class QualifyingTarget:
    """One ``(language, target)`` pair that qualifies for downmixing."""

    language: str
    target: DownmixTarget


def detect_qualifying_targets(
    streams: Sequence[AudioStreamInfo], settings: DownmixSettings
) -> list[QualifyingTarget]:
    """Return the ``(language, target)`` pairs that qualify for downmixing.

    Streams are grouped by language (the literal ``"unknown"`` tag from
    :func:`~collapsarr.downmix.probe.probe_audio_streams` is treated as any
    other language). For each language, a target qualifies when it is
    enabled, its channel count is strictly less than that language's
    highest existing channel count, and no existing stream for that
    language already sits at the target's exact channel count.

    Results are ordered by language (alphabetically) then by ascending
    target channel count (Stereo, 2.1, 5.1), so stacked targets for a
    single language appear in a stable, predictable order.

    When ``settings.language_allow_list`` is set, streams whose language
    isn't in it are excluded from consideration entirely — that language
    simply doesn't appear in the result, no error is raised.
    """
    channels_by_language: dict[str, set[int]] = defaultdict(set)
    for stream in streams:
        if (
            settings.language_allow_list is not None
            and stream.language not in settings.language_allow_list
        ):
            continue
        channels_by_language[stream.language].add(stream.channels)

    qualifying: list[QualifyingTarget] = []
    for language in sorted(channels_by_language):
        existing_channel_counts = channels_by_language[language]
        max_channels = max(existing_channel_counts)

        for target in _TARGET_ORDER:
            if target not in settings.enabled_targets:
                continue
            target_channels = _TARGET_CHANNELS[target]
            if target_channels >= max_channels:
                continue  # would upmix, or duplicate an existing/higher tier
            if target_channels in existing_channel_counts:
                continue  # already present for this language
            qualifying.append(QualifyingTarget(language=language, target=target))

    return qualifying
