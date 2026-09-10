"""Direct Plex API write for the Default Audio Track Job's immediate triggers (COL-245).

:mod:`~collapsarr.downmix.default_audio_pipeline` fixes a file's Default Audio
Track disposition by remuxing it with ffmpeg -- a local file mutation, safe
even when Collapsarr can't reach Plex at all, but slower and unnecessary when
it can. This module is the alternative mechanism for when a Plex Connection
*is* configured: instead of touching the file, it writes the change directly
to the Plex Media Server that already has the file open, via its own
per-stream "set default audio" API.

In order, for one file:

1. **Resolve the file's Plex ``ratingKey``**
   (:func:`~collapsarr.plex.library_sync.resolve_rating_key`) -- the mapping
   table first, falling back to a single live query on a miss. A live-query
   *hit* is written back into the mapping table immediately by that function
   (COL-245's other half, see :mod:`collapsarr.plex.library_sync`'s module
   docstring) rather than waiting for the next Plex Sync. A **miss** (both the
   table and the live-query fallback failing) is, unlike every other caller of
   ``resolve_rating_key``, a **hard failure** here -- :attr:`PlexDefaultAudio
   Outcome.RATING_KEY_UNRESOLVED` -- not a silent no-op: a Default Audio Track
   Job that can't find its own file's Plex counterpart has nothing to apply
   its fix to, and that must be visible as a Job failure, not swallowed.
2. **GET** the item's current stream list
   (:func:`~collapsarr.plex.client.get_item_metadata`, parsed by
   :func:`~collapsarr.plex.streams.parse_audio_streams`). When the caller
   passes ``expected_stream_count`` (COL-251 -- a downmix-triggered Job
   only; see below), fewer reported streams than that is a distinct hard
   failure (:attr:`PlexDefaultAudioOutcome.STREAM_NOT_YET_INGESTED`) rather
   than falling through to resolution: Plex's asynchronous ingestion of the
   remux's newly-added stream(s) hasn't caught up yet, so resolving against
   this stale list could otherwise silently apply the fix to the *wrong*
   (pre-existing) stream instead of failing loudly.
3. **Resolve** the preferred stream over that list
   (:func:`~collapsarr.plex.default_audio.resolve_default_audio_stream`,
   COL-240). Fewer than two streams to compare resolves to ``None`` --
   reported as :attr:`PlexDefaultAudioOutcome.NOTHING_TO_DO`, a non-error
   success, no write attempted -- mirroring :func:`~collapsarr.downmix.
   default_audio_pipeline.run_default_audio_pipeline`'s own no-op contract.
4. **PUT** the set-default write against the resolved stream's id and its
   containing Part's id (``winner.part_id`` -- Plex's set-default-audio-track
   write targets the Part, not the item's ``ratingKey``; see
   :func:`~collapsarr.plex.client.set_default_audio_stream`'s docstring,
   COL-252).
5. **GET-verify**: fetch the item again and confirm the resolved stream is
   now flagged ``selected`` before reporting success. A verifying GET that
   itself fails to fetch is a distinct failure
   (:attr:`PlexDefaultAudioOutcome.VERIFY_FETCH_FAILED`) from one that fetches
   fine but doesn't confirm the write
   (:attr:`PlexDefaultAudioOutcome.VERIFY_MISMATCH`) -- the former means "we
   don't know if the write took", the latter means "we know it didn't".

No local file mutation happens anywhere on this path -- no ffprobe, no
ffmpeg, no remux -- only HTTP calls against the Plex server and the
mapping-table cache write :func:`~collapsarr.plex.library_sync.
resolve_rating_key` may make.

This module intentionally does not decide *when* it runs -- gating on
whether a Plex Connection is configured, and choosing between this mechanism
and the ffmpeg-remux fallback, is a separate mechanism-selection concern
(COL-247) layered on top. This module implements the mechanism itself for
every trigger: the immediate ones (manual/bulk trigger, plain new-file-ready
webhook trigger) call it with no ``expected_stream_count``; the
downmix-triggered, delayed case (COL-251) passes one, arming the
stream-not-yet-ingested check in step 2 above.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from collapsarr.downmix.default_audio import DefaultAudioPreference

from .client import get_item_metadata, set_default_audio_stream
from .default_audio import resolve_default_audio_stream
from .library_sync import resolve_rating_key
from .streams import PlexAudioStream, parse_audio_streams

logger = logging.getLogger(__name__)


class PlexDefaultAudioOutcome(Enum):
    """Which stage of the direct-Plex-API-write sequence a result came from."""

    SUCCESS = "success"
    NOTHING_TO_DO = "nothing_to_do"
    RATING_KEY_UNRESOLVED = "rating_key_unresolved"
    STREAM_FETCH_FAILED = "stream_fetch_failed"
    STREAM_NOT_YET_INGESTED = "stream_not_yet_ingested"
    #: COL-253 -- explicit per-track mode only (``explicit_stream_index`` set):
    #: Plex currently reports fewer audio streams than ``explicit_stream_index``
    #: requires. Mirrors ``STREAM_NOT_YET_INGESTED``'s "hard-fail on a
    #: stream-count mismatch rather than guess" pattern, for the same reason --
    #: silently resolving against the wrong ordinal position would apply the
    #: fix to the wrong track.
    STREAM_INDEX_OUT_OF_RANGE = "stream_index_out_of_range"
    WRITE_FAILED = "write_failed"
    VERIFY_FETCH_FAILED = "verify_fetch_failed"
    VERIFY_MISMATCH = "verify_mismatch"


@dataclass(frozen=True, slots=True)
class PlexDefaultAudioResult:
    """Structured outcome of one :func:`apply_default_audio_via_plex` run.

    ``success`` is ``True`` only for :attr:`PlexDefaultAudioOutcome.SUCCESS`
    and :attr:`PlexDefaultAudioOutcome.NOTHING_TO_DO` -- a no-op (fewer than
    two streams to compare) is reported separately via ``outcome`` rather
    than folded into failure, since nothing was attempted and nothing failed,
    mirroring :class:`~collapsarr.downmix.pipeline.PipelineResult`'s own
    ``NOTHING_TO_DO``/``success`` split.

    ``rating_key``/``stream_id`` are populated as soon as they're known, even
    on a later failure, so a caller building Job history has them for
    context. ``detail`` is always a human-readable one-line summary, specific
    and distinguishable per outcome (per COL-245's acceptance criteria).
    """

    outcome: PlexDefaultAudioOutcome
    success: bool
    detail: str
    rating_key: str | None = None
    stream_id: str | None = None


def _finish(result: PlexDefaultAudioResult, level: int) -> PlexDefaultAudioResult:
    """Log ``result.detail`` at ``level`` and return ``result`` (mirrors COL-129 elsewhere)."""
    logger.log(level, result.detail)
    return result


def apply_default_audio_via_plex(
    session: Session,
    file_path: str | Path,
    preference: DefaultAudioPreference,
    *,
    base_url: str,
    token: str,
    transport: httpx.BaseTransport | None = None,
    expected_stream_count: int | None = None,
    explicit_stream_index: int | None = None,
) -> PlexDefaultAudioResult:
    """Fix a single file's Default Audio Track disposition via a direct Plex API write.

    See the module docstring for the full GET -> resolve -> PUT -> GET-verify
    sequence and what each failure mode means. Never raises: every stage's
    failure is captured as a :class:`PlexDefaultAudioResult`, the same
    "capture, don't raise" contract every other Plex-facing module in this
    package (:mod:`collapsarr.plex.client`, :mod:`collapsarr.plex.
    library_sync`) already follows.

    ``expected_stream_count`` (COL-251) is the file's total audio-stream
    count as of the downmix remux that triggered this Job (see
    :attr:`~collapsarr.downmix.pipeline.PipelineResult.final_stream_count`),
    passed only for a downmix-triggered ``SET_DEFAULT_AUDIO`` Job -- ``None``
    (the default) for every immediate trigger, which acts on streams that
    already exist and so has nothing to wait on. When set, and Plex currently
    reports *fewer* audio streams than this for the item, the write is
    aborted with :attr:`PlexDefaultAudioOutcome.STREAM_NOT_YET_INGESTED`
    before any resolution or write is attempted.

    ``explicit_stream_index`` (COL-253) is the file detail page's per-row
    "Set Default Audio" trigger's *explicit per-track* mode -- see
    :attr:`~collapsarr.jobs.queue.Job.explicit_stream_index` for exactly what
    it means (an ordinal position within the audio-only stream list, *not* a
    Plex stream id -- Plex has no ffprobe-index counterpart at all, which is
    exactly why ordinal position, not id-matching, is how this maps the
    request onto one of Plex's reported streams) and why it exists. ``None``
    (the default) is every other trigger, unchanged: the ordinary
    auto-resolve mode below. When set:

    - :func:`~collapsarr.plex.default_audio.resolve_default_audio_stream` is
      never called -- the target stream is ``streams[explicit_stream_index]``
      directly, a plain list index into this same call's freshly-fetched,
      audio-only ``streams``. Fewer Plex-reported audio streams than
      ``explicit_stream_index`` requires is a hard failure
      (:attr:`PlexDefaultAudioOutcome.STREAM_INDEX_OUT_OF_RANGE`) rather than
      guessing which stream was meant -- the same defensive pattern as the
      ``expected_stream_count`` check above.
    - There is no "nothing to do" no-op here either: this mode always
      attempts the write, unconditionally, even when Plex already reports
      the target stream as ``selected``. That is the one behavior this whole
      mode exists for: re-triggering a file Collapsarr already believes is
      correct (e.g. after a Plex-API write that silently failed under the
      now-fixed COL-252 bug).

    Every stage after stream selection (PUT, GET-verify) is unchanged
    either way.
    """
    path_str = str(file_path)

    rating_key = resolve_rating_key(
        session, path_str, base_url=base_url, token=token, transport=transport
    )
    if rating_key is None:
        return _finish(
            PlexDefaultAudioResult(
                outcome=PlexDefaultAudioOutcome.RATING_KEY_UNRESOLVED,
                success=False,
                detail=(
                    f"Could not resolve a Plex ratingKey for {path_str!r}: not found in the "
                    "Plex Library Item cache and no matching live Plex search result; no "
                    "Plex API write attempted"
                ),
            ),
            logging.ERROR,
        )

    metadata_result = get_item_metadata(base_url, token, rating_key, transport=transport)
    if not metadata_result.ok:
        return _finish(
            PlexDefaultAudioResult(
                outcome=PlexDefaultAudioOutcome.STREAM_FETCH_FAILED,
                success=False,
                detail=(
                    f"Failed to fetch Plex stream metadata for ratingKey {rating_key} "
                    f"(file {path_str!r}): {metadata_result.error}"
                ),
                rating_key=rating_key,
            ),
            logging.ERROR,
        )

    streams = parse_audio_streams(metadata_result.payload)
    if expected_stream_count is not None and len(streams) < expected_stream_count:
        return _finish(
            PlexDefaultAudioResult(
                outcome=PlexDefaultAudioOutcome.STREAM_NOT_YET_INGESTED,
                success=False,
                detail=(
                    f"Plex reports {len(streams)} audio stream(s) for ratingKey {rating_key} "
                    f"(file {path_str!r}), fewer than the {expected_stream_count} expected "
                    "after the downmix remux; Plex likely hasn't finished ingesting the new "
                    "stream yet -- no Plex API write attempted"
                ),
                rating_key=rating_key,
            ),
            logging.ERROR,
        )

    if explicit_stream_index is not None:
        # COL-253: explicit per-track mode -- direct list index, no
        # resolver, no "already correct"/"nothing to do" check. See the
        # docstring above.
        if not 0 <= explicit_stream_index < len(streams):
            return _finish(
                PlexDefaultAudioResult(
                    outcome=PlexDefaultAudioOutcome.STREAM_INDEX_OUT_OF_RANGE,
                    success=False,
                    detail=(
                        f"Plex reports {len(streams)} audio stream(s) for ratingKey "
                        f"{rating_key} (file {path_str!r}), fewer than the requested stream "
                        f"ordinal {explicit_stream_index} requires; no Plex API write attempted"
                    ),
                    rating_key=rating_key,
                ),
                logging.ERROR,
            )
        winner: PlexAudioStream = streams[explicit_stream_index]
    else:
        resolved = resolve_default_audio_stream(streams, preference)
        if resolved is None:
            return _finish(
                PlexDefaultAudioResult(
                    outcome=PlexDefaultAudioOutcome.NOTHING_TO_DO,
                    success=True,
                    detail=(
                        f"Fewer than two audio streams to compare for ratingKey {rating_key} "
                        f"(file {path_str!r}); nothing to do"
                    ),
                    rating_key=rating_key,
                ),
                logging.WARNING,
            )
        winner = resolved

    write_result = set_default_audio_stream(
        base_url, token, winner.part_id, winner.id, transport=transport
    )
    if not write_result.ok:
        return _finish(
            PlexDefaultAudioResult(
                outcome=PlexDefaultAudioOutcome.WRITE_FAILED,
                success=False,
                detail=(
                    f"Plex set-default-audio write failed for ratingKey {rating_key}, "
                    f"streamID {winner.id} (file {path_str!r}): {write_result.error}"
                ),
                rating_key=rating_key,
                stream_id=winner.id,
            ),
            logging.ERROR,
        )

    verify_result = get_item_metadata(base_url, token, rating_key, transport=transport)
    if not verify_result.ok:
        return _finish(
            PlexDefaultAudioResult(
                outcome=PlexDefaultAudioOutcome.VERIFY_FETCH_FAILED,
                success=False,
                detail=(
                    f"Plex set-default-audio write for ratingKey {rating_key}, streamID "
                    f"{winner.id} (file {path_str!r}) could not be verified: the confirming "
                    f"GET failed: {verify_result.error}"
                ),
                rating_key=rating_key,
                stream_id=winner.id,
            ),
            logging.ERROR,
        )

    verify_streams = parse_audio_streams(verify_result.payload)
    confirmed = any(stream.id == winner.id and stream.selected for stream in verify_streams)
    if not confirmed:
        return _finish(
            PlexDefaultAudioResult(
                outcome=PlexDefaultAudioOutcome.VERIFY_MISMATCH,
                success=False,
                detail=(
                    f"Plex set-default-audio write for ratingKey {rating_key} did not take "
                    f"effect: streamID {winner.id} (file {path_str!r}) is not flagged "
                    "selected after verification"
                ),
                rating_key=rating_key,
                stream_id=winner.id,
            ),
            logging.ERROR,
        )

    return PlexDefaultAudioResult(
        outcome=PlexDefaultAudioOutcome.SUCCESS,
        success=True,
        detail=(
            f"Default Audio Track set via direct Plex API write for ratingKey {rating_key}, "
            f"streamID {winner.id} (file {path_str!r})"
        ),
        rating_key=rating_key,
        stream_id=winner.id,
    )
