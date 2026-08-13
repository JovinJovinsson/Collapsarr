"""ORM models for the Plex integration (COL-209 connection, COL-210 item map).

Two tables live here:

* :class:`PlexConnection` (COL-209) -- the **singleton** connection row.
  Unlike :class:`~collapsarr.arr.models.ArrInstance` (a CRUD list -- any number
  of Sonarr/Radarr connections), Collapsarr talks to exactly one Plex Media
  Server, so this is modelled as a singleton, mirroring
  :class:`~collapsarr.notify.models.NotifierConfig`'s convention: the ``id``
  column is constrained to always equal :data:`PLEX_CONNECTION_ID`, so at most
  one row can ever exist. :mod:`collapsarr.plex.service` is the only intended
  way to read or create it.

  ``base_url``/``token`` are the only fields required to talk to the server;
  the ``status``/``status_error``/``status_checked_at``/``version`` columns
  cache the result of the last connectivity check performed by the service
  layer on save -- the same treatment :class:`ArrInstance` gives its own
  connectivity columns. ``token`` (the ``X-Plex-Token``) is a server-side-only
  secret: it is never included in any HTTP response -- see
  :mod:`collapsarr.plex.routes`, whose read schema omits the column entirely
  (only a derived ``has_token`` boolean is exposed).

* :class:`PlexLibraryItem` (COL-210) -- one row per known on-disk file path,
  mapping it to the Plex library item (``rating_key`` + ``section_key``) that
  contains it. Rebuilt **wholesale** on every Plex Sync run (see
  :mod:`collapsarr.plex.library_sync`); it is pure derived state (no
  operator-owned columns to preserve, unlike
  :class:`~collapsarr.library.models.LibraryNode`'s ``tracked_override``), so a
  full delete-and-reinsert is the whole update model.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base

PLEX_CONNECTION_ID = 1
"""The fixed primary key of the single :class:`PlexConnection` row."""


class ConnectivityStatus(enum.StrEnum):
    """Outcome of the most recent connectivity/version check for the Plex server.

    A separate enum from :class:`collapsarr.arr.models.ConnectivityStatus`
    (same three values) rather than a shared import, keeping the ``plex``
    package self-contained the way ``notify``/``settings`` don't reach into
    ``arr`` either -- see the DB-level enum name (``plex_connectivity_status``,
    set in the migration) for how the two stay distinct at the schema level
    too.
    """

    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PlexConnection(Base):
    """The single row of persisted Plex Media Server connection config.

    ``base_url``/``token`` default to empty strings so the row can be
    get-or-created (via :func:`collapsarr.plex.service.get_plex_connection`)
    before an operator has configured anything -- an empty-string
    connectivity check simply fails fast (captured as ``status="error"``,
    never raised), the same way an unreachable URL does for
    :class:`~collapsarr.arr.models.ArrInstance`.
    """

    __tablename__ = "plex_connection"
    __table_args__ = (
        CheckConstraint(f"id = {PLEX_CONNECTION_ID}", name="ck_plex_connection_singleton"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=PLEX_CONNECTION_ID)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    token: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    status: Mapped[ConnectivityStatus] = mapped_column(
        SAEnum(
            ConnectivityStatus,
            name="plex_connectivity_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=ConnectivityStatus.UNKNOWN,
    )
    status_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    status_checked_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    version: Mapped[str | None] = mapped_column(String(50), nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return (
            f"PlexConnection(id={self.id!r}, base_url={self.base_url!r}, "
            f"status={self.status!r})"
        )


class PlexLibraryItem(Base):
    """One mapping from a tracked file's on-disk path to its Plex library item (COL-210).

    Keyed by :attr:`file_path` (unique) -- the same on-disk path a
    :class:`~collapsarr.media.models.TrackedMediaFile` and a
    :class:`~collapsarr.jobs.history.JobHistory` row use -- so a file's Plex
    ``ratingKey`` can be looked up directly by the path Collapsarr already
    knows it by, with no path parsing or catalog round-trip. :attr:`rating_key`
    is Plex's own per-item id (a string; Plex numbers them but treats them
    opaquely) and :attr:`section_key` is the library section it lives in.

    The whole table is **rebuilt wholesale** on every Plex Sync run
    (:func:`collapsarr.plex.library_sync.rebuild_library_items`): a sync deletes
    every row and re-inserts one per item it walks. There is no
    ``tracked_override``-style operator state to preserve here (unlike
    :class:`~collapsarr.library.models.LibraryNode`, which soft-hides rather
    than deletes), so delete-and-reinsert is the correct, simplest update
    model -- a file that has moved or left Plex simply doesn't reappear.
    """

    __tablename__ = "plex_library_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True, index=True)
    #: Plex's own opaque per-item id (its ``ratingKey``), stored as the string
    #: Plex returns it as -- never coerced to an int, since Plex treats it as
    #: an opaque handle.
    rating_key: Mapped[str] = mapped_column(String(100), nullable=False)
    #: The Plex library section (its ``key``) this item belongs to.
    section_key: Mapped[str] = mapped_column(String(50), nullable=False)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return (
            f"PlexLibraryItem(id={self.id!r}, file_path={self.file_path!r}, "
            f"rating_key={self.rating_key!r}, section_key={self.section_key!r})"
        )
