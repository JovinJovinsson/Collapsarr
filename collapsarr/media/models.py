"""ORM models for tracked media files and their per-target/per-language downmix status (COL-25).

Per the product design (``docs/plans/2026-07-20-collapsarr-v1-design.md``),
"Tracked media" is: a file path, its source channel layout(s) by language,
and a per-target (Stereo/2.1/5.1) per-language status of *missing*,
*processed*, or *excluded-by-language-filter*. This module is the persisted
form of that: :class:`TrackedMediaFile` is one row per known media file
(keyed by its unique file path), and :class:`TrackedMediaTargetStatus` is
one row per ``(language, target)`` pair on that file -- the granularity the
future "Wanted" view (which lists files missing at least one enabled
target, COL-28) and the downmix job queue (which needs to know exactly
which language/target combos still need a track added) both need.

:class:`~collapsarr.downmix.targets.DownmixTarget` is reused for the
``target`` column rather than inventing a parallel enum, the same way
:mod:`collapsarr.settings.models` reuses it for ``GlobalSettings.
enabled_targets`` (there, comma-joined into one column; here, one row per
target so status can differ per-target).

:mod:`collapsarr.media.service` is the only intended way to create, update,
or query these rows -- nothing here touches a session.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base
from collapsarr.downmix.targets import DownmixTarget


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MediaTargetStatus(enum.StrEnum):
    """Status of one ``(language, target)`` pair on a tracked media file.

    Matches the three states the product design calls out verbatim:
    ``missing`` (the target qualifies for a new downmixed track -- see
    :func:`~collapsarr.downmix.targets.detect_qualifying_targets` -- and no
    downmix job has produced it yet), ``processed`` (the target already
    exists on the file, whether because a downmix job completed or because
    the file already contained that channel layout), and
    ``excluded_by_language_filter`` (the language isn't in the settings'
    language allow-list, so this target was never evaluated for it).
    """

    MISSING = "missing"
    PROCESSED = "processed"
    EXCLUDED_BY_LANGUAGE_FILTER = "excluded_by_language_filter"


class TrackedMediaFile(Base):
    """One tracked media file, identified by its unique local file path.

    Created/updated by :func:`collapsarr.media.service.upsert_tracked_media`
    from a scan (:mod:`collapsarr.arr.files`) or webhook
    (:mod:`collapsarr.arr.webhooks`) event; its per-target-per-language
    status lives in :class:`TrackedMediaTargetStatus` rows rather than on
    this row directly, since a single file can have many languages, each
    with its own per-target status.

    :attr:`instance_id` + :attr:`sonarr_episode_id`/:attr:`radarr_movie_id`
    (COL-101) are the bridge back to this file's owning
    :class:`~collapsarr.library.models.LibraryNode` -- the unit **Tracked**
    (``CONTEXT.md``) actually lives on. All three are nullable and
    populated *when known* (a scan or webhook event carries the Arr
    instance's own object ids; a manual trigger by bare file path does
    not), and deliberately Arr-object-id-keyed rather than derived from
    :attr:`file_path` -- ``CONTEXT.md`` rules out parsing on-disk paths to
    infer catalog identity, since paths are unstable/remappable and don't
    durably identify a catalog object across the scan/webhook mirror
    boundary. Exactly one of :attr:`sonarr_episode_id`/:attr:`radarr_movie_id`
    is set, matching :attr:`instance_id`'s Arr instance type -- the same
    one-of-two-nullable-columns shape :class:`~collapsarr.library.models.LibraryNode`
    already uses for the same distinction.
    """

    __tablename__ = "tracked_media_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True, index=True)

    #: The Arr instance this file was last scanned/webhooked from. ``NULL`` if
    #: never resolved via a scan/webhook (e.g. only ever manually triggered by
    #: bare path). ``ondelete="SET NULL"`` here is a *schema-level* FK
    #: declaration only -- a tracked file's downmix history is meant to
    #: outlive the Arr instance that originally reported it, rather than
    #: cascading away with it -- consistent with how
    #: :class:`~collapsarr.library.models.LibraryNode` declares its own
    #: ``instance_id``/``parent_id`` FKs. Like those, whether this is actually
    #: *enforced* at runtime depends on SQLite's ``PRAGMA foreign_keys`` being
    #: enabled, which :mod:`collapsarr.database` does not currently do (see
    #: COL-98/99's tracked FK-enforcement follow-up) -- so
    #: :func:`collapsarr.arr.service.delete_instance` deleting an
    #: :class:`~collapsarr.arr.models.ArrInstance` does *not* currently null
    #: this column out; it can be left pointing at a since-deleted instance
    #: id until that follow-up lands.
    instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("arr_instances.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Sonarr's own episode id (COL-101), set when this file came from a
    #: Sonarr instance. ``NULL`` on a Radarr file or an unresolved row.
    sonarr_episode_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    #: Radarr's own movie id (COL-101), set when this file came from a Radarr
    #: instance. ``NULL`` on a Sonarr file or an unresolved row.
    radarr_movie_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    #: The language of the audio stream that currently carries the
    #: container's Default Audio Track disposition, as of the most recent
    #: probe through an existing call site (scan, webhook import, manual
    #: trigger -- COL-154). Mirrors
    #: :attr:`~collapsarr.downmix.probe.AudioStreamInfo.language`'s own
    #: normalization (including the literal ``"unknown"`` bucket for an
    #: untagged stream), so it is *not* itself an "unset" sentinel --
    #: ``NULL`` here specifically means "never probed since this shipped, or
    #: the file's ffprobe metadata carries no ``disposition.default`` flag on
    #: any stream at all" (the latter is indistinguishable from the former at
    #: read time, and both render the same "unknown" state on the Library
    #: page). Refreshed unconditionally on every
    #: :func:`~collapsarr.media.service.upsert_tracked_media` call, the same
    #: single write path every probe call site already funnels through.
    current_default_language: Mapped[str | None] = mapped_column(String(50), nullable=True)
    #: The channel layout of that same stream
    #: (:attr:`~collapsarr.downmix.probe.AudioStreamInfo.channel_layout`,
    #: e.g. ``"5.1"``, ``"stereo"``, or the ``"<channels>ch"`` fallback) --
    #: paired with :attr:`current_default_language` to render the Library
    #: page's current Default Audio Track column (e.g. "Danish · 5.1").
    #: ``NULL`` under the exact same conditions as
    #: :attr:`current_default_language` -- the two are always written or left
    #: ``NULL`` together, never independently.
    current_default_channel_layout: Mapped[str | None] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return f"TrackedMediaFile(id={self.id!r}, file_path={self.file_path!r})"


class TrackedMediaTargetStatus(Base):
    """The status of one ``(language, target)`` pair for a :class:`TrackedMediaFile`.

    Unique on ``(media_id, language, target)`` so
    :func:`collapsarr.media.service.upsert_tracked_media` can upsert in
    place rather than accumulating duplicate rows across repeated scans of
    the same file. ``language`` matches the tag
    :func:`~collapsarr.downmix.probe.probe_audio_streams` normalizes
    streams to (including the literal ``"unknown"`` bucket for untagged
    streams).
    """

    __tablename__ = "tracked_media_target_status"
    __table_args__ = (
        UniqueConstraint(
            "media_id",
            "language",
            "target",
            name="uq_tracked_media_target_status_media_language_target",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[int] = mapped_column(
        ForeignKey("tracked_media_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    language: Mapped[str] = mapped_column(String(50), nullable=False)
    target: Mapped[DownmixTarget] = mapped_column(
        SAEnum(
            DownmixTarget,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    status: Mapped[MediaTargetStatus] = mapped_column(
        SAEnum(
            MediaTargetStatus,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return (
            f"TrackedMediaTargetStatus(id={self.id!r}, media_id={self.media_id!r}, "
            f"language={self.language!r}, target={self.target!r}, status={self.status!r})"
        )
