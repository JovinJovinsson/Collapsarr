"""Service-layer upsert + query for tracked media (COL-25).

Plain functions taking a SQLAlchemy :class:`~sqlalchemy.orm.Session`,
matching the pattern used throughout this codebase
(:mod:`collapsarr.arr.service`, :mod:`collapsarr.jobs.history`,
:mod:`collapsarr.settings.service`). HTTP exposure (the future Wanted-view
API, COL-28) is a separate ticket's concern -- this module is the whole
surface.

Two write paths, matching the ticket's first two acceptance criteria:

- :func:`upsert_tracked_media` -- called from a scan
  (:mod:`collapsarr.arr.files`) or webhook (:mod:`collapsarr.arr.webhooks`)
  event with a file path and its freshly-probed
  (:func:`collapsarr.downmix.probe.probe_audio_streams`) per-stream
  metadata. Recomputes every ``(language, target)`` status row from
  scratch, reusing :func:`~collapsarr.downmix.targets.detect_qualifying_targets`
  for the missing/not-missing decision -- so it's naturally idempotent and
  self-correcting: a target already present on the file (whether an
  original track or the product of a previous downmix job) is never
  re-flagged ``missing``.
- :func:`record_target_processed` -- called as a downmix job completes, to
  flip a single ``(language, target)`` pair to ``processed`` directly,
  without needing a full rescan first.

:func:`list_files_missing_targets` is the read path: "files missing at
least one enabled target" -- the data source the future Wanted view
(COL-28) reads from.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget, detect_qualifying_targets

from .models import MediaTargetStatus, TrackedMediaFile, TrackedMediaTargetStatus


def _channels_by_language(streams: Sequence[AudioStreamInfo]) -> dict[str, set[int]]:
    """Group stream channel counts by language.

    Mirrors :func:`~collapsarr.downmix.targets.detect_qualifying_targets`'s
    own grouping, so the two stay in lockstep over which languages exist on
    a file.
    """
    channels_by_language: dict[str, set[int]] = defaultdict(set)
    for stream in streams:
        channels_by_language[stream.language].add(stream.channels)
    return channels_by_language


def get_tracked_media(session: Session, file_path: str | Path) -> TrackedMediaFile | None:
    """Return the tracked media row for ``file_path``, or ``None`` if not yet tracked."""
    return session.scalars(
        select(TrackedMediaFile).where(TrackedMediaFile.file_path == str(file_path))
    ).one_or_none()


def list_tracked_media(session: Session) -> list[TrackedMediaFile]:
    """Return every tracked media file, ordered by id (insertion order)."""
    return list(session.scalars(select(TrackedMediaFile).order_by(TrackedMediaFile.id)))


def list_target_statuses(
    session: Session, file_path: str | Path
) -> list[TrackedMediaTargetStatus]:
    """Return every per-``(language, target)`` status row for ``file_path``.

    Returns an empty list both when the file isn't tracked at all and when
    it's tracked but has no status rows yet -- callers that need to tell
    those cases apart should call :func:`get_tracked_media` first.
    """
    media = get_tracked_media(session, file_path)
    if media is None:
        return []
    return list(
        session.scalars(
            select(TrackedMediaTargetStatus)
            .where(TrackedMediaTargetStatus.media_id == media.id)
            .order_by(TrackedMediaTargetStatus.id)
        )
    )


def upsert_tracked_media(
    session: Session,
    *,
    file_path: str | Path,
    streams: Sequence[AudioStreamInfo],
    settings: DownmixSettings,
) -> TrackedMediaFile:
    """Create or update a tracked media row from freshly-probed stream metadata.

    Creates the :class:`~collapsarr.media.models.TrackedMediaFile` row if
    ``file_path`` isn't tracked yet (matched by exact path string, the same
    convention :mod:`collapsarr.jobs.history` uses for
    ``JobHistory.file_path``), else reuses the existing row. For every
    language present in ``streams`` and every target enabled in
    ``settings``, upserts a
    :class:`~collapsarr.media.models.TrackedMediaTargetStatus` row:

    - ``EXCLUDED_BY_LANGUAGE_FILTER`` if ``settings.language_allow_list`` is
      set and doesn't include the language;
    - ``MISSING`` if :func:`~collapsarr.downmix.targets.detect_qualifying_targets`
      says the ``(language, target)`` pair still qualifies for a new track;
    - ``PROCESSED`` otherwise (the target already exists on the file --
      either an original track or the product of a previous downmix job).

    Only targets enabled in ``settings`` get a status row; a target that
    was tracked while enabled and later disabled keeps its last-known row
    rather than being deleted -- it simply stops being counted once a
    caller's ``enabled_targets`` (see :func:`list_files_missing_targets`)
    no longer includes it. Safe to call repeatedly for the same file: rows
    are updated in place, never duplicated (enforced by
    :class:`~collapsarr.media.models.TrackedMediaTargetStatus`'s
    ``(media_id, language, target)`` unique constraint).
    """
    path_str = str(file_path)
    media = get_tracked_media(session, path_str)
    if media is None:
        media = TrackedMediaFile(file_path=path_str)
        session.add(media)
        session.flush()

    channels_by_language = _channels_by_language(streams)
    qualifying = {(qt.language, qt.target) for qt in detect_qualifying_targets(streams, settings)}
    existing_rows = {
        (row.language, row.target): row
        for row in session.scalars(
            select(TrackedMediaTargetStatus).where(TrackedMediaTargetStatus.media_id == media.id)
        )
    }

    for language in channels_by_language:
        for target in settings.enabled_targets:
            key = (language, target)
            if (
                settings.language_allow_list is not None
                and language not in settings.language_allow_list
            ):
                status = MediaTargetStatus.EXCLUDED_BY_LANGUAGE_FILTER
            elif key in qualifying:
                status = MediaTargetStatus.MISSING
            else:
                status = MediaTargetStatus.PROCESSED

            row = existing_rows.get(key)
            if row is None:
                row = TrackedMediaTargetStatus(
                    media_id=media.id, language=language, target=target, status=status
                )
                session.add(row)
            else:
                row.status = status

    session.commit()
    session.refresh(media)
    return media


def record_target_processed(
    session: Session,
    *,
    file_path: str | Path,
    language: str,
    target: DownmixTarget,
) -> TrackedMediaTargetStatus:
    """Flip a single ``(language, target)`` pair to ``PROCESSED`` as a downmix job completes.

    The job queue's completion hook calls this directly (no full rescan
    needed) so status reflects a successful downmix immediately; a later
    scan calling :func:`upsert_tracked_media` will independently arrive at
    the same ``PROCESSED`` state once it re-probes the file and sees the
    new track, so the two write paths never disagree.

    Get-or-creates both the :class:`~collapsarr.media.models.TrackedMediaFile`
    row and the target status row if either doesn't exist yet, so this is
    safe to call even if a job somehow completes before the file was ever
    scanned.
    """
    path_str = str(file_path)
    media = get_tracked_media(session, path_str)
    if media is None:
        media = TrackedMediaFile(file_path=path_str)
        session.add(media)
        session.flush()

    row = session.scalars(
        select(TrackedMediaTargetStatus).where(
            TrackedMediaTargetStatus.media_id == media.id,
            TrackedMediaTargetStatus.language == language,
            TrackedMediaTargetStatus.target == target,
        )
    ).one_or_none()
    if row is None:
        row = TrackedMediaTargetStatus(
            media_id=media.id,
            language=language,
            target=target,
            status=MediaTargetStatus.PROCESSED,
        )
        session.add(row)
    else:
        row.status = MediaTargetStatus.PROCESSED

    session.commit()
    session.refresh(row)
    return row


def list_files_missing_targets(
    session: Session, *, enabled_targets: frozenset[DownmixTarget]
) -> list[TrackedMediaFile]:
    """Return every tracked file missing at least one of ``enabled_targets``.

    ``enabled_targets`` is taken explicitly (rather than re-derived from
    stored rows) so a settings change is reflected immediately without
    requiring every tracked file to be rescanned first -- a target no
    longer in ``enabled_targets`` is excluded from the check even if a
    stale ``MISSING`` row for it still exists. Ordered by id (insertion
    order), matching the rest of the service layer; a file with multiple
    missing targets/languages appears once.
    """
    stmt = (
        select(TrackedMediaFile)
        .join(TrackedMediaTargetStatus, TrackedMediaTargetStatus.media_id == TrackedMediaFile.id)
        .where(
            TrackedMediaTargetStatus.status == MediaTargetStatus.MISSING,
            TrackedMediaTargetStatus.target.in_(enabled_targets),
        )
        .order_by(TrackedMediaFile.id)
        .distinct()
    )
    return list(session.scalars(stmt))
