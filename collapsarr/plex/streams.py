"""Parse per-part audio ``Stream`` elements out of a Plex metadata response (COL-240).

A future ticket adds a :mod:`collapsarr.plex.client` call that fetches
``GET /library/metadata/{ratingKey}`` and returns the raw response payload;
this module is the parser that turns that payload into a flat list of
:class:`PlexAudioStream`
records -- the Plex-reported equivalent of
:class:`~collapsarr.downmix.probe.AudioStreamInfo`, which
:mod:`collapsarr.plex.default_audio` (COL-240) resolves a Preferred Default
Audio winner from, and a later ticket writes back to Plex as a
``audioStreamID=<PlexAudioStream.id>`` set-default call.

A Plex metadata response nests streams three levels deep --
``MediaContainer.Metadata[].Media[].Part[].Stream[]`` -- with every stream
type (video, audio, subtitle) mixed into one ``Stream`` list, distinguished
by ``streamType`` (``2`` is audio, per Plex's own convention). This module
flattens that nesting and filters to audio only, mirroring
:func:`collapsarr.downmix.probe._parse_audio_streams`'s "skip anything not
matching the expected type/shape" defensiveness rather than raising on a
malformed or unexpected payload.

Pure parsing, no I/O -- the HTTP call itself is a later ticket's concern (see
the module docstring above); this only operates on an already-fetched
payload, matching COL-240's "no behavior change yet" scope.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Plex's numeric ``streamType`` for an audio stream (``1`` is video, ``3`` is
#: subtitle) -- the discriminator used to filter a ``Stream`` list down to
#: audio-only entries.
_AUDIO_STREAM_TYPE = 2

#: Same "no usable language tag" bucket
#: :mod:`collapsarr.downmix.probe` normalizes ffprobe streams into, so a
#: Plex-reported stream with no ``languageCode`` (or Plex's own "undetermined"
#: placeholder) compares equal to an untagged local stream under the same
#: :class:`~collapsarr.downmix.default_audio.DefaultAudioPreference`.
_UNKNOWN_LANGUAGE = "unknown"
_UNDETERMINED_LANGUAGE_CODES = {"und"}


@dataclass(frozen=True, slots=True)
class PlexAudioStream:
    """One audio ``Stream`` entry from a Plex metadata response.

    ``id`` is Plex's own opaque per-stream id -- the value the set-default
    write passes back as the ``audioStreamID`` query param -- kept as the
    string Plex sends it as, never coerced to an int, the same treatment
    :class:`~collapsarr.plex.client.PlexMediaItem.rating_key` gives Plex's
    item ids. ``part_id`` is the id of the containing ``Media[].Part[]``
    entry this stream was nested under (same string-coercion treatment as
    ``id``) -- Plex's real set-default-audio-track write targets the *Part*,
    not the item's ``ratingKey`` (COL-252), so this is the value
    :func:`~collapsarr.plex.client.set_default_audio_stream` needs, not
    ``id`` alone. ``language`` is normalized the same way
    :func:`collapsarr.downmix.probe._normalize_language` normalizes ffprobe's
    ``tags.language``: lowercased, with a missing or "und" tag folded into
    ``"unknown"`` -- so :mod:`collapsarr.plex.default_audio` compares
    Plex-reported and locally-probed languages on equal footing. ``title``/
    ``extended_display_title`` are Plex's own free-text labels (e.g. a
    commentary track's title, or "English (AC3 5.1)"), each ``None`` when
    Plex omits it. ``selected`` mirrors Plex's ``selected`` flag: whether
    this stream is the part's *current* default audio stream.
    """

    id: str
    part_id: str
    channels: int
    language: str
    title: str | None
    extended_display_title: str | None
    selected: bool


def parse_audio_streams(payload: object) -> list[PlexAudioStream]:
    """Flatten every audio ``Stream`` out of a ``GET /library/metadata/{ratingKey}`` payload.

    Walks ``MediaContainer.Metadata[].Media[].Part[].Stream[]``, keeping only
    entries whose ``streamType`` is audio (:data:`_AUDIO_STREAM_TYPE`).
    Defensive against malformed/unexpected shapes the same way
    :func:`collapsarr.downmix.probe._parse_audio_streams` is: anything not
    matching the expected type is skipped rather than raising, and an
    unparseable top-level payload (or one missing ``MediaContainer``) simply
    yields an empty list. A multi-part item's streams are concatenated across
    parts in encounter order.
    """
    if not isinstance(payload, dict):
        return []

    container = payload.get("MediaContainer")
    if not isinstance(container, dict):
        return []

    results: list[PlexAudioStream] = []
    for entry in _as_dict_list(container.get("Metadata")):
        for media in _as_dict_list(entry.get("Media")):
            for part in _as_dict_list(media.get("Part")):
                part_id = _coerce_id(part.get("id"))
                if part_id is None:
                    # No usable Part id -- the set-default write has nothing
                    # to target for any stream nested under it, so skip the
                    # whole part rather than parsing streams we can't act on.
                    continue
                for stream in _as_dict_list(part.get("Stream")):
                    parsed = _parse_audio_stream(stream, part_id)
                    if parsed is not None:
                        results.append(parsed)

    return results


def _as_dict_list(value: object) -> list[dict[str, object]]:
    """Return ``value`` as a list of dicts, dropping any non-dict entries; ``[]`` otherwise."""
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _parse_audio_stream(stream: dict[str, object], part_id: str) -> PlexAudioStream | None:
    """Parse one ``Stream`` entry into a :class:`PlexAudioStream`, or ``None`` if unusable."""
    if stream.get("streamType") != _AUDIO_STREAM_TYPE:
        return None

    raw_id = _coerce_id(stream.get("id"))
    if raw_id is None:
        return None

    channels = stream.get("channels")
    if not isinstance(channels, int):
        return None

    return PlexAudioStream(
        id=raw_id,
        part_id=part_id,
        channels=channels,
        language=_normalize_language(stream.get("languageCode")),
        title=_optional_str(stream.get("title")),
        extended_display_title=_optional_str(stream.get("extendedDisplayTitle")),
        selected=bool(stream.get("selected")),
    )


def _coerce_id(value: object) -> str | None:
    """Coerce a Plex ``id`` (int or non-empty string) to ``str``, else ``None``.

    Shared by both the ``Stream`` id and the containing ``Part`` id -- Plex's
    JSON sends either shape for either field.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value:
        return None
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _normalize_language(raw_language_code: object) -> str:
    if isinstance(raw_language_code, str) and raw_language_code.strip():
        language = raw_language_code.strip().lower()
        if language not in _UNDETERMINED_LANGUAGE_CODES:
            return language
    return _UNKNOWN_LANGUAGE


def is_commentary_track(stream: PlexAudioStream) -> bool:
    """Whether ``stream`` looks like a commentary track, from title alone (COL-246).

    ``True`` when either ``title`` or ``extended_display_title`` contains
    "comment", case-insensitively. Plex exposes no native commentary
    disposition the way ffprobe's ``disposition.comment`` flag does for a
    locally-probed stream (see
    :func:`collapsarr.downmix.probe.is_commentary_track`'s other signal), so
    this is title-only — a single-signal check across the two free-text
    fields rather than the local side's two-signal OR.
    """
    for candidate in (stream.title, stream.extended_display_title):
        if candidate is not None and "comment" in candidate.lower():
            return True
    return False
