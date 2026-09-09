"""Refresh the Default Audio Track display snapshot from Plex on every Plex Sync (COL-248).

:mod:`collapsarr.media.service`'s :func:`~collapsarr.media.service.
upsert_tracked_media` keeps :class:`~collapsarr.media.models.TrackedMediaFile`'s
``current_default_language``/``current_default_channel_layout`` columns (COL-154)
in lockstep with the *local* ffprobe-reported stream list, but only at the
existing probe call sites (scan, webhook import, manual trigger) -- it never
runs on its own. That leaves a gap: a default track changed directly in
Plex's own UI, outside Collapsarr entirely, never re-probes the file locally,
so the snapshot silently drifts stale.

This module closes that gap from the *other* direction. It reuses the exact
Plex Stream model/read path COL-245's direct-Plex-API-write already built --
:func:`~collapsarr.plex.client.get_item_metadata` (the ``GET
/library/metadata/{ratingKey}`` call) parsed by
:func:`~collapsarr.plex.streams.parse_audio_streams` -- and, for every
tracked file the Plex Sync mapping table (:mod:`collapsarr.plex.library_sync`)
currently resolves to a ``ratingKey``, writes Plex's own *currently selected*
stream (:attr:`~collapsarr.plex.streams.PlexAudioStream.selected`) back into
those same two columns. Driven once per :class:`~collapsarr.plex.scheduler.
PlexSyncScheduler` tick, right after :func:`~collapsarr.plex.library_sync.
rebuild_library_items` refreshes the mapping table that this function reads
-- so a run always sees the current tick's resolution, not a stale one from
the previous week.

Only **tracked, Plex-resolved** files are touched -- an inner join between
:class:`~collapsarr.media.models.TrackedMediaFile` and
:class:`~collapsarr.plex.models.PlexLibraryItem` on ``file_path`` -- mirroring
the ticket's acceptance criteria verbatim. A file that isn't tracked, or has
no current Plex mapping (never matched during the rebuild, or genuinely not
in Plex), is left untouched; there is nothing to refresh it from.

A per-file metadata-fetch failure (a transient Plex hiccup, or an item that
vanished from Plex between the mapping rebuild and this pass) is soft-failed:
that one file's existing snapshot is left as-is and the sync moves on to the
next, mirroring :func:`~collapsarr.plex.library_sync.rebuild_library_items`'s
own "never let a partial failure destroy otherwise-good data" stance. A
successful fetch reporting *no* selected stream at all (an empty stream list,
or none flagged ``selected``) is a genuine "unknown" outcome -- both snapshot
columns are set to ``None``, the same "unknown" rendering
:func:`collapsarr.media.service._current_default_stream` gives an
un-flagged local probe.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from collapsarr.media.models import TrackedMediaFile

from .client import ItemMetadataResult, get_item_metadata
from .models import PlexLibraryItem
from .streams import PlexAudioStream, parse_audio_streams

logger = logging.getLogger(__name__)

#: Injectable client-call seam, defaulting to the real
#: :func:`~collapsarr.plex.client.get_item_metadata` -- mirrors
#: :mod:`collapsarr.plex.library_sync`'s own ``list_sections``/``list_items``
#: seams, so this can be driven in tests with an in-memory fake instead of a
#: real Plex server.
GetMetadataFn = Callable[..., ItemMetadataResult]

#: Named channel-count tiers this codebase already has a fixed vocabulary
#: for (:class:`~collapsarr.downmix.targets.DownmixTarget`'s own values),
#: mirrored here as a private, module-local copy rather than imported --
#: matching :mod:`collapsarr.plex.default_audio`'s own ``_TARGET_CHANNELS``
#: copy (see that module's docstring for why each module keeps its own).
#: A channel count outside this map falls back to ffprobe's own
#: ``"<channels>ch"`` convention (:func:`collapsarr.downmix.probe.
#: _normalize_channel_layout`'s fallback), so every count still renders
#: *something* on the Library page rather than nothing.
_CHANNEL_LAYOUT_NAMES: dict[int, str] = {2: "stereo", 3: "2.1", 6: "5.1"}


def _channel_layout_for(channels: int) -> str:
    """Map a Plex-reported channel count to the same display vocabulary ffprobe uses."""
    return _CHANNEL_LAYOUT_NAMES.get(channels, f"{channels}ch")


def _selected_stream(streams: list[PlexAudioStream]) -> PlexAudioStream | None:
    """Return the stream Plex currently flags ``selected``, if any.

    On the rare malformed item reporting more than one selected stream, the
    first one wins -- picking a single deterministic winner is what matters
    here, mirroring :func:`collapsarr.media.service._current_default_stream`'s
    own tie-break stance (there is no channel-count signal to prefer one
    over another the way :mod:`collapsarr.plex.default_audio`'s preference
    resolution does elsewhere in this package).
    """
    for stream in streams:
        if stream.selected:
            return stream
    return None


def refresh_default_audio_snapshots(
    session: Session,
    *,
    base_url: str,
    token: str,
    transport: object | None = None,
    get_metadata: GetMetadataFn = get_item_metadata,
) -> int:
    """Refresh every tracked, Plex-resolved file's Default Audio Track display snapshot.

    Returns the number of :class:`~collapsarr.media.models.TrackedMediaFile`
    rows whose snapshot columns were written (successfully fetched and
    parsed, whether that landed a stream or the "unknown" ``None``/``None``
    pair) -- not the number of rows *changed*, matching
    :func:`~collapsarr.plex.library_sync.rebuild_library_items`'s own
    "count of rows processed" return convention.

    A blank ``base_url`` (Plex not configured) is a no-op, returning ``0``
    without touching any row -- there is nothing to refresh from.
    """
    if not base_url:
        return 0

    rows = session.execute(
        select(TrackedMediaFile, PlexLibraryItem.rating_key).join(
            PlexLibraryItem, PlexLibraryItem.file_path == TrackedMediaFile.file_path
        )
    ).all()

    updated = 0
    for media, rating_key in rows:
        metadata_result = get_metadata(base_url, token, rating_key, transport=transport)
        if not metadata_result.ok:
            logger.debug(
                "Plex sync: could not refresh Default Audio Track snapshot for %r "
                "(ratingKey %s): %s; leaving its snapshot unchanged",
                media.file_path,
                rating_key,
                metadata_result.error,
            )
            continue

        selected = _selected_stream(parse_audio_streams(metadata_result.payload))
        media.current_default_language = selected.language if selected else None
        media.current_default_channel_layout = (
            _channel_layout_for(selected.channels) if selected else None
        )
        updated += 1

    session.commit()
    return updated


__all__ = ["GetMetadataFn", "refresh_default_audio_snapshots"]
