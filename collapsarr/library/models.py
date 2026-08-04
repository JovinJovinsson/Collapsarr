"""ORM model for a Library node -- a Sonarr Series/Season/Episode mirror (COL-98).

A **Library** (``CONTEXT.md``) is a per-``ArrInstance`` mirror of that instance's
Sonarr catalog, persisted in Collapsarr's own database. A **Library Node** is one
entry in that mirror's tree: a Series, Season, or Episode. All three kinds share
one self-referential table (:class:`LibraryNode`) rather than three parallel
tables, because the **Tracked** flag's ancestor-override resolution and
cascade-on-toggle both walk the same Series > Season > Episode tree uniformly --
a single ``parent_id`` chain makes that walk trivial.

Nodes are identified by Sonarr's *own* object ids, never parsed from on-disk
paths: a Series carries Sonarr's series id, an Episode its episode id. Sonarr has
no standalone season object, so a Season's identity is ``(series_id,
season_number)``. To dedup upserts cleanly across all three kinds under one
unique constraint -- SQLite treats ``NULL`` as distinct, so a
mixed-nullability composite key would not dedup Series rows -- each node stores a
single deterministic :attr:`node_key` string (see :func:`make_node_key`), unique
per instance.

Each node carries:

- :attr:`tracked_override` -- a *nullable* Tracked override. ``None`` means
  "inherit" (resolve from the nearest ancestor with an explicit value, falling
  back to ``GlobalSettings.default_tracked``); ``True``/``False`` is an explicit
  per-node setting. Resolution and cascade live in
  :mod:`collapsarr.library.service`.
- :attr:`hidden` -- the soft-hide marker. A node a later scan no longer reports
  is hidden, not deleted (:mod:`collapsarr.library.service`), preserving its
  ``tracked_override`` in case it reappears.

:mod:`collapsarr.library.service` is the only intended way to create, update, or
query these rows -- nothing here touches a session.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base


class LibraryNodeKind(enum.StrEnum):
    """The three kinds of Sonarr Library node."""

    SERIES = "series"
    SEASON = "season"
    EPISODE = "episode"


def make_node_key(
    kind: LibraryNodeKind,
    *,
    series_id: int,
    season_number: int | None = None,
    episode_id: int | None = None,
) -> str:
    """Return the deterministic per-instance identity key for a node.

    Encodes Sonarr's own ids so repeated scans upsert the same row rather than
    duplicating it:

    - Series -> ``"series:<series_id>"``
    - Season -> ``"season:<series_id>:<season_number>"``
    - Episode -> ``"episode:<episode_id>"``
    """
    if kind is LibraryNodeKind.SERIES:
        return f"series:{series_id}"
    if kind is LibraryNodeKind.SEASON:
        return f"season:{series_id}:{season_number}"
    return f"episode:{episode_id}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LibraryNode(Base):
    """One Series, Season, or Episode node in an instance's Library tree.

    Scoped to an :class:`~collapsarr.arr.models.ArrInstance` via ``instance_id``
    (cascade-deleted with it) and linked into a Series > Season > Episode tree
    via the self-referential ``parent_id`` (``None`` for a Series root). The
    ``(instance_id, node_key)`` pair is unique, so an upsert keyed on it is
    idempotent across repeated scans.
    """

    __tablename__ = "library_nodes"
    __table_args__ = (
        UniqueConstraint("instance_id", "node_key", name="uq_library_nodes_instance_node_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("arr_instances.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_nodes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[LibraryNodeKind] = mapped_column(
        SAEnum(
            LibraryNodeKind,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        index=True,
    )
    node_key: Mapped[str] = mapped_column(String(100), nullable=False)

    #: Sonarr's own series id -- present on all three kinds, so a series' whole
    #: subtree can be scoped by it.
    sonarr_series_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    #: Season number (Season + Episode nodes); ``None`` on a Series node.
    season_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Episode number (Episode nodes); ``None`` otherwise.
    episode_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Sonarr's own episode id (Episode nodes); ``None`` otherwise.
    sonarr_episode_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Whether this node's file exists in Sonarr (Episode nodes). Series/Season
    #: nodes are always ``False`` -- they are containers, not files.
    has_file: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Nullable Tracked override: ``None`` inherits; ``True``/``False`` is explicit.
    tracked_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    #: Soft-hide marker: a node a later scan no longer reports is hidden, not deleted.
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return (
            f"LibraryNode(id={self.id!r}, instance_id={self.instance_id!r}, "
            f"kind={self.kind!r}, node_key={self.node_key!r}, "
            f"tracked_override={self.tracked_override!r}, hidden={self.hidden!r})"
        )
