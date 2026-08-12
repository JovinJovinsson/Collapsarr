"""Service-layer sync, Tracked resolution, cascade, and tree read for the Library (COL-98/COL-99).

Plain functions over a SQLAlchemy :class:`~sqlalchemy.orm.Session`, matching the
pattern used throughout this codebase (:mod:`collapsarr.arr.service`,
:mod:`collapsarr.media.service`, :mod:`collapsarr.settings.service`). This module
is the whole surface over :class:`~collapsarr.library.models.LibraryNode`;
nothing else creates or mutates those rows.

Four responsibilities:

- :func:`sync_library` -- upsert every node from a freshly-fetched
  :class:`~collapsarr.arr.catalog.SonarrCatalog` (Series > Season > Episode) or
  :class:`~collapsarr.arr.catalog.RadarrCatalog` (flat Movie, COL-99) and
  **soft-hide** (never delete) any previously-synced node the catalog no
  longer reports. Idempotent and self-correcting: a reappearing node is
  un-hidden with its ``tracked_override`` intact. Newly-discovered nodes are
  created with ``tracked_override = None`` (inherit) -- a new episode under a
  Not-Tracked series (or a newly-discovered movie) therefore resolves to Not
  Tracked automatically whenever the global default is, without copying an
  ancestor's value down.
- :func:`resolve_tracked` -- the ancestor-override resolution: the nearest
  explicit override walking up from a node wins; when nothing in the ancestry is
  explicit, ``GlobalSettings.default_tracked`` is the fallback. A Movie node has
  no ancestor, so this is just its own override or the global default.
- :func:`set_tracked` -- write an explicit override on a node and, for a Series
  or Season, **cascade**: overwrite every existing descendant's override to
  match. Exposed via ``POST /api/library/tracked``
  (:mod:`collapsarr.library.routes`, COL-101), which resolves each request's
  ``{node_type, node_id}`` reference to a node and calls this function per
  reference.
- :func:`build_tree` / :func:`build_movie_tree` -- the read paths behind
  ``GET /api/library/instances/{id}/tree``: the visible Series > Season >
  Episode tree, or the visible flat Movie list, each node carrying its
  *resolved* Tracked value. Hidden nodes are omitted from both. Each
  Episode/Movie leaf also carries its current-default-track snapshot
  (COL-154) and its bridged tracked-media file id (COL-194), both bridged
  from :mod:`collapsarr.media.service`'s tracked-media rows the same way
  Tracked resolution bridges the other direction: one bulk fetch of every
  tracked-media row for the instance
  (:func:`~collapsarr.media.service.list_tracked_media_by_instance`), keyed
  by the same ``sonarr_episode_id``/``radarr_movie_id`` the tree already
  carries, rather than a per-node query.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from collapsarr.arr.catalog import RadarrCatalog, SonarrCatalog
from collapsarr.settings.service import get_global_settings

from .models import LibraryNode, LibraryNodeKind, make_node_key

if TYPE_CHECKING:
    # Deferred to a function-local import at the two call sites below (and kept
    # here only for static typing): collapsarr.media.service itself imports
    # this module (get_node_by_source_id/list_nodes/resolve_tracked, the
    # Tracked-write bridge, COL-101) to reach the Library node a tracked-media
    # row belongs to, so importing collapsarr.media.* at module scope here
    # would be circular -- collapsarr.media.__init__ eagerly imports
    # .service, which eagerly imports .library.service, before this module
    # would finish defining the names media.service is trying to import.
    from collapsarr.media.models import TrackedMediaFile


class LibraryNodeNotFoundError(LookupError):
    """Raised when an operation targets a library-node id that does not exist."""


# --- read DTOs ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TreeDefaultTrack:
    """A file-bearing leaf node's current Default Audio Track snapshot (COL-154).

    Mirrors :class:`~collapsarr.media.models.TrackedMediaFile`'s
    ``current_default_language``/``current_default_channel_layout`` columns,
    refreshed at every existing probe call site (scan, webhook import, manual
    trigger). Rendered by the Library page as e.g. "Dan · 5.1".
    """

    language: str
    channel_layout: str


@dataclass(frozen=True, slots=True)
class TreeEpisode:
    """An Episode node in a rendered Library tree, with its resolved Tracked value."""

    id: int
    sonarr_episode_id: int
    season_number: int
    episode_number: int
    title: str
    has_file: bool
    tracked: bool
    #: ``None`` when the file hasn't been probed since COL-154 shipped, or
    #: its ffprobe metadata carries no Default Audio Track disposition flag
    #: on any stream -- both render as "unknown" on the Library page.
    current_default_track: TreeDefaultTrack | None
    #: The bridged :class:`~collapsarr.media.models.TrackedMediaFile` row's
    #: id (COL-194) -- the same id the Wanted page's file detail route
    #: (``/wanted/:fileId``) matches against. ``None`` whenever there is no
    #: bridged row yet (mirrors :func:`_adapt_default_track`'s ``None``
    #: case), which in practice only happens when ``has_file`` is ``False``
    #: or the file hasn't been scanned/imported since COL-154 shipped --
    #: never populated from ``has_file`` alone, since that flag comes from
    #: the Arr catalog, not this bridge.
    file_id: int | None


@dataclass(frozen=True, slots=True)
class TreeSeason:
    """A Season node in a rendered Library tree, with its resolved Tracked value."""

    id: int
    season_number: int
    tracked: bool
    episodes: tuple[TreeEpisode, ...]


@dataclass(frozen=True, slots=True)
class TreeSeries:
    """A Series node in a rendered Library tree, with its resolved Tracked value."""

    id: int
    sonarr_series_id: int
    title: str
    tracked: bool
    seasons: tuple[TreeSeason, ...]


@dataclass(frozen=True, slots=True)
class LibraryTree:
    """A Sonarr instance's full visible Library tree with resolved Tracked values."""

    instance_id: int
    series: tuple[TreeSeries, ...]


@dataclass(frozen=True, slots=True)
class TreeMovie:
    """A Movie node in a rendered Library tree, with its resolved Tracked value."""

    id: int
    radarr_movie_id: int
    title: str
    has_file: bool
    tracked: bool
    #: See :attr:`TreeEpisode.current_default_track` (COL-154).
    current_default_track: TreeDefaultTrack | None
    #: See :attr:`TreeEpisode.file_id` (COL-194).
    file_id: int | None


@dataclass(frozen=True, slots=True)
class MovieLibraryTree:
    """A Radarr instance's full visible, flat Movie list with resolved Tracked values."""

    instance_id: int
    movies: tuple[TreeMovie, ...]


# --- queries -----------------------------------------------------------------


def list_nodes(
    session: Session, instance_id: int, *, include_hidden: bool = True
) -> list[LibraryNode]:
    """Return an instance's library nodes, ordered by id (insertion order).

    ``include_hidden`` defaults to ``True`` because both sync and Tracked
    resolution need the full set (a hidden ancestor's override still resolves);
    the tree read path filters hidden nodes out itself.
    """
    stmt = select(LibraryNode).where(LibraryNode.instance_id == instance_id)
    if not include_hidden:
        stmt = stmt.where(LibraryNode.hidden.is_(False))
    return list(session.scalars(stmt.order_by(LibraryNode.id)))


def get_node(session: Session, node_id: int) -> LibraryNode | None:
    """Return the library node with ``node_id``, or ``None`` if it doesn't exist."""
    return session.get(LibraryNode, node_id)


def get_node_by_source_id(
    session: Session,
    *,
    instance_id: int,
    sonarr_episode_id: int | None = None,
    radarr_movie_id: int | None = None,
) -> LibraryNode | None:
    """Return the Episode/Movie node matching an Arr instance's own object id (COL-101).

    The bridge :mod:`collapsarr.media`'s ``TrackedMediaFile`` (keyed only by
    file path) uses to find *its* owning node -- and so its Tracked value --
    without parsing ``file_path`` (``CONTEXT.md`` rules that out: paths
    aren't a stable catalog identity). Exactly one of ``sonarr_episode_id``/
    ``radarr_movie_id`` is expected to be given, matching ``instance_id``'s
    Arr instance type; if neither is given, there is nothing to look up and
    this returns ``None`` without querying. Scoped by ``instance_id`` because
    Sonarr/Radarr's own object ids are only unique *within* one instance, not
    globally.
    """
    if sonarr_episode_id is not None:
        stmt = select(LibraryNode).where(
            LibraryNode.instance_id == instance_id,
            LibraryNode.kind == LibraryNodeKind.EPISODE,
            LibraryNode.sonarr_episode_id == sonarr_episode_id,
        )
    elif radarr_movie_id is not None:
        stmt = select(LibraryNode).where(
            LibraryNode.instance_id == instance_id,
            LibraryNode.kind == LibraryNodeKind.MOVIE,
            LibraryNode.radarr_movie_id == radarr_movie_id,
        )
    else:
        return None
    return session.scalars(stmt).one_or_none()


# --- Tracked resolution ------------------------------------------------------


def resolve_tracked(
    node: LibraryNode, nodes_by_id: dict[int, LibraryNode], default_tracked: bool
) -> bool:
    """Resolve ``node``'s effective Tracked value.

    Walks up the ``parent_id`` chain and returns the first explicit
    ``tracked_override`` found (the node's own wins over its ancestors');
    ``default_tracked`` (the global setting) is the fallback when nothing in the
    ancestry is explicit.
    """
    current: LibraryNode | None = node
    while current is not None:
        if current.tracked_override is not None:
            return current.tracked_override
        if current.parent_id is None:
            break
        current = nodes_by_id.get(current.parent_id)
    return default_tracked


# --- current-default-track enrichment (COL-154) ------------------------------


def _adapt_default_track(media: TrackedMediaFile | None) -> TreeDefaultTrack | None:
    """Adapt a bridged :class:`~collapsarr.media.models.TrackedMediaFile`'s snapshot columns.

    ``None`` when there is no bridged row at all (never probed/scanned via a
    call site that captured this node's catalog ids), or when the row exists
    but its snapshot columns are themselves ``NULL`` (never probed since
    COL-154 shipped, or no stream reports the disposition flag) -- both are
    the same "unknown" outcome from the Library page's point of view.
    """
    if media is None or media.current_default_language is None:
        return None
    assert media.current_default_channel_layout is not None  # written together, see the model
    return TreeDefaultTrack(
        language=media.current_default_language,
        channel_layout=media.current_default_channel_layout,
    )


# --- sync --------------------------------------------------------------------


def sync_library(
    session: Session, *, instance_id: int, catalog: SonarrCatalog | RadarrCatalog
) -> None:
    """Upsert every node from ``catalog`` and soft-hide those it no longer reports.

    Dispatches on the catalog type: a :class:`~collapsarr.arr.catalog.SonarrCatalog`
    upserts the Series > Season > Episode tree, a
    :class:`~collapsarr.arr.catalog.RadarrCatalog` (COL-99) upserts the flat
    Movie list. Both share the same mechanics: existing nodes are matched by
    ``node_key`` and updated in place with their ``tracked_override``
    preserved; a previously-hidden node reappearing in the catalog is
    un-hidden; new nodes are created inheriting (``tracked_override = None``);
    and any existing node whose ``node_key`` is absent from ``catalog`` is
    marked ``hidden`` -- never deleted -- so its Tracked value survives an
    upstream deletion and returns if the node is seen again.
    """
    if isinstance(catalog, RadarrCatalog):
        _sync_radarr_catalog(session, instance_id=instance_id, catalog=catalog)
    else:
        _sync_sonarr_catalog(session, instance_id=instance_id, catalog=catalog)


def _sync_sonarr_catalog(session: Session, *, instance_id: int, catalog: SonarrCatalog) -> None:
    """Upsert every Series/Season/Episode node from ``catalog`` (see :func:`sync_library`)."""
    existing = {node.node_key: node for node in list_nodes(session, instance_id)}
    seen: set[str] = set()

    for series in catalog.series:
        series_key = make_node_key(LibraryNodeKind.SERIES, series_id=series.series_id)
        seen.add(series_key)
        series_node = _upsert_node(
            session,
            existing,
            instance_id=instance_id,
            kind=LibraryNodeKind.SERIES,
            node_key=series_key,
            parent_id=None,
            sonarr_series_id=series.series_id,
            title=series.title,
        )
        session.flush()

        season_nodes: dict[int, LibraryNode] = {}
        for season_number in series.season_numbers:
            season_key = make_node_key(
                LibraryNodeKind.SEASON,
                series_id=series.series_id,
                season_number=season_number,
            )
            seen.add(season_key)
            season_nodes[season_number] = _upsert_node(
                session,
                existing,
                instance_id=instance_id,
                kind=LibraryNodeKind.SEASON,
                node_key=season_key,
                parent_id=series_node.id,
                sonarr_series_id=series.series_id,
                season_number=season_number,
                title=f"Season {season_number}",
            )
        session.flush()

        for episode in series.episodes:
            episode_key = make_node_key(
                LibraryNodeKind.EPISODE,
                series_id=series.series_id,
                episode_id=episode.episode_id,
            )
            seen.add(episode_key)
            parent = season_nodes.get(episode.season_number)
            _upsert_node(
                session,
                existing,
                instance_id=instance_id,
                kind=LibraryNodeKind.EPISODE,
                node_key=episode_key,
                parent_id=parent.id if parent is not None else series_node.id,
                sonarr_series_id=series.series_id,
                season_number=episode.season_number,
                episode_number=episode.episode_number,
                sonarr_episode_id=episode.episode_id,
                title=episode.title,
                has_file=episode.has_file,
            )

    _soft_hide_missing(existing, seen)
    session.commit()


def _sync_radarr_catalog(session: Session, *, instance_id: int, catalog: RadarrCatalog) -> None:
    """Upsert every Movie node from ``catalog`` (see :func:`sync_library`).

    Flat -- Movie nodes have no parent (``parent_id=None``) and no
    season/episode level, unlike the Sonarr Series > Season > Episode tree.
    """
    existing = {node.node_key: node for node in list_nodes(session, instance_id)}
    seen: set[str] = set()

    for movie in catalog.movies:
        movie_key = make_node_key(LibraryNodeKind.MOVIE, movie_id=movie.movie_id)
        seen.add(movie_key)
        _upsert_node(
            session,
            existing,
            instance_id=instance_id,
            kind=LibraryNodeKind.MOVIE,
            node_key=movie_key,
            parent_id=None,
            radarr_movie_id=movie.movie_id,
            title=movie.title,
            has_file=movie.has_file,
        )

    _soft_hide_missing(existing, seen)
    session.commit()


def _soft_hide_missing(existing: dict[str, LibraryNode], seen: set[str]) -> None:
    """Mark any existing node whose key wasn't ``seen`` in this sync as hidden.

    A malformed-but-200 catalog response (COL-136) is guarded against
    upstream, not here: :mod:`collapsarr.arr.catalog` raises
    :class:`~collapsarr.arr.catalog.MalformedCatalogResponse` rather than
    silently returning an empty catalog for a non-list payload, so a
    genuinely empty ``seen`` reaching this function means the fetch
    actually succeeded and reported nothing -- soft-hiding everything is
    the correct, intentional behavior in that case.
    """
    for node_key, node in existing.items():
        if node_key not in seen and not node.hidden:
            node.hidden = True


def _upsert_node(
    session: Session,
    existing: dict[str, LibraryNode],
    *,
    instance_id: int,
    kind: LibraryNodeKind,
    node_key: str,
    parent_id: int | None,
    sonarr_series_id: int | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
    sonarr_episode_id: int | None = None,
    radarr_movie_id: int | None = None,
    title: str,
    has_file: bool = False,
) -> LibraryNode:
    """Create ``node_key`` or update it in place; never touch ``tracked_override``.

    Un-hides a reappearing node. Newly-created nodes inherit
    (``tracked_override`` left ``None``). Registers new nodes into ``existing``
    so sibling/child lookups within the same sync see them.
    """
    node = existing.get(node_key)
    if node is None:
        node = LibraryNode(
            instance_id=instance_id,
            kind=kind,
            node_key=node_key,
            parent_id=parent_id,
            sonarr_series_id=sonarr_series_id,
            season_number=season_number,
            episode_number=episode_number,
            sonarr_episode_id=sonarr_episode_id,
            radarr_movie_id=radarr_movie_id,
            title=title,
            has_file=has_file,
        )
        session.add(node)
        existing[node_key] = node
        return node

    node.parent_id = parent_id
    node.season_number = season_number
    node.episode_number = episode_number
    node.sonarr_episode_id = sonarr_episode_id
    node.radarr_movie_id = radarr_movie_id
    node.title = title
    node.has_file = has_file
    node.hidden = False
    return node


# --- incremental (webhook) upsert --------------------------------------------


def upsert_series_episode_node(
    session: Session,
    *,
    instance_id: int,
    series_id: int,
    series_title: str,
    season_number: int,
    episode_id: int,
    episode_number: int,
    episode_title: str,
    has_file: bool = True,
) -> LibraryNode:
    """Upsert the Series > Season > Episode chain for one imported episode (COL-102).

    The real-time, single-path counterpart to :func:`sync_library`'s
    full-catalog Sonarr pass: a webhook import event
    (:mod:`collapsarr.arr.webhooks`) carries exactly one episode's catalog
    coordinates, and this upserts just its three-node ancestry
    (Series > Season > Episode) so the Library mirror reflects the new file
    immediately rather than only at the next periodic scan (ADR-0002).

    Reuses :func:`_upsert_node`, so it inherits the same guarantees: existing
    nodes are matched by ``node_key`` and updated in place with their
    ``tracked_override`` preserved, a previously-hidden node reappearing is
    un-hidden, and a brand-new node is created inheriting
    (``tracked_override = None``). Unlike :func:`sync_library` it never
    soft-hides -- a single import says nothing about which *other* nodes still
    exist, so it only ever adds/updates the one chain. Returns the Episode
    (leaf) node, which is what the Tracked-gate bridge (COL-101)
    :func:`get_node_by_source_id` resolves back to.
    """
    existing = {node.node_key: node for node in list_nodes(session, instance_id)}

    series_node = _upsert_node(
        session,
        existing,
        instance_id=instance_id,
        kind=LibraryNodeKind.SERIES,
        node_key=make_node_key(LibraryNodeKind.SERIES, series_id=series_id),
        parent_id=None,
        sonarr_series_id=series_id,
        title=series_title,
    )
    session.flush()

    season_node = _upsert_node(
        session,
        existing,
        instance_id=instance_id,
        kind=LibraryNodeKind.SEASON,
        node_key=make_node_key(
            LibraryNodeKind.SEASON, series_id=series_id, season_number=season_number
        ),
        parent_id=series_node.id,
        sonarr_series_id=series_id,
        season_number=season_number,
        title=f"Season {season_number}",
    )
    session.flush()

    episode_node = _upsert_node(
        session,
        existing,
        instance_id=instance_id,
        kind=LibraryNodeKind.EPISODE,
        node_key=make_node_key(LibraryNodeKind.EPISODE, episode_id=episode_id),
        parent_id=season_node.id,
        sonarr_series_id=series_id,
        season_number=season_number,
        episode_number=episode_number,
        sonarr_episode_id=episode_id,
        title=episode_title,
        has_file=has_file,
    )
    session.commit()
    return episode_node


def upsert_movie_node(
    session: Session,
    *,
    instance_id: int,
    movie_id: int,
    title: str,
    has_file: bool = True,
) -> LibraryNode:
    """Upsert one Movie node from a Radarr import webhook (COL-102).

    The flat Radarr counterpart to :func:`upsert_series_episode_node` -- a
    Movie node has no ancestor level. Same non-soft-hiding, ``tracked_override``-
    preserving :func:`_upsert_node` semantics; returns the Movie node.
    """
    existing = {node.node_key: node for node in list_nodes(session, instance_id)}
    movie_node = _upsert_node(
        session,
        existing,
        instance_id=instance_id,
        kind=LibraryNodeKind.MOVIE,
        node_key=make_node_key(LibraryNodeKind.MOVIE, movie_id=movie_id),
        parent_id=None,
        radarr_movie_id=movie_id,
        title=title,
        has_file=has_file,
    )
    session.commit()
    return movie_node


# --- Tracked write + cascade -------------------------------------------------


def set_tracked(
    session: Session, *, node_id: int, tracked: bool, commit: bool = True
) -> LibraryNode:
    """Set an explicit Tracked override on a node, cascading to descendants.

    Writes ``node.tracked_override = tracked``. When the node is a Series or
    Season, every existing descendant node's ``tracked_override`` is overwritten
    to the same value -- the cascade-on-toggle rule. (An Episode has no
    descendants, so only its own override changes.)

    ``commit`` defaults to ``True`` (this function's original, standalone
    behaviour: commit and refresh before returning). A caller that needs to
    apply several of these writes as one all-or-nothing unit -- e.g.
    ``POST /api/library/tracked``'s bulk endpoint
    (:mod:`collapsarr.library.routes`) -- passes ``commit=False`` to flush the
    write into the *current* transaction without ending it, so a later write
    in the same batch failing can still roll every earlier one in that batch
    back. ``node`` is not refreshed in that case: ``tracked_override`` (and
    any cascaded descendants') were just set in Python above, so the caller's
    view of it is already correct without a round-trip; a full refresh
    would additionally require an autoflush of the *uncommitted* write to
    stay consistent, which this session factory disables
    (:func:`collapsarr.database.create_session_factory`).

    Raises:
        LibraryNodeNotFoundError: if ``node_id`` does not exist.
    """
    node = session.get(LibraryNode, node_id)
    if node is None:
        raise LibraryNodeNotFoundError(f"No library node with id={node_id}")

    node.tracked_override = tracked

    if node.kind in (LibraryNodeKind.SERIES, LibraryNodeKind.SEASON):
        children_by_parent: dict[int, list[LibraryNode]] = defaultdict(list)
        for candidate in list_nodes(session, node.instance_id):
            if candidate.parent_id is not None:
                children_by_parent[candidate.parent_id].append(candidate)

        stack = list(children_by_parent[node.id])
        while stack:
            descendant = stack.pop()
            descendant.tracked_override = tracked
            stack.extend(children_by_parent[descendant.id])

    if commit:
        session.commit()
        session.refresh(node)
    else:
        session.flush()
    return node


# --- tree read ---------------------------------------------------------------


def build_tree(session: Session, instance_id: int) -> LibraryTree:
    """Build the visible Series > Season > Episode tree with resolved Tracked values.

    Hidden nodes are excluded from the output, but the *full* node set (hidden
    included) backs Tracked resolution so an override on a hidden ancestor still
    applies. Series are ordered by title, seasons by season number, episodes by
    episode number. Each Episode leaf also carries its current-default-track
    snapshot (COL-154, see :func:`_adapt_default_track`), bridged from one bulk
    fetch of the instance's tracked-media rows keyed by ``sonarr_episode_id``.
    """
    # Local import: see the TYPE_CHECKING note above -- module-scope would be circular.
    from collapsarr.media.service import list_tracked_media_by_instance

    default_tracked = get_global_settings(session).default_tracked
    nodes = list_nodes(session, instance_id)
    nodes_by_id = {node.id: node for node in nodes}

    media_by_episode_id = {
        media.sonarr_episode_id: media
        for media in list_tracked_media_by_instance(session, instance_id)
        if media.sonarr_episode_id is not None
    }

    seasons_by_parent: dict[int, list[LibraryNode]] = defaultdict(list)
    episodes_by_parent: dict[int, list[LibraryNode]] = defaultdict(list)
    series_nodes: list[LibraryNode] = []
    for node in nodes:
        if node.hidden:
            continue
        if node.kind is LibraryNodeKind.SERIES:
            series_nodes.append(node)
        elif node.kind is LibraryNodeKind.SEASON and node.parent_id is not None:
            seasons_by_parent[node.parent_id].append(node)
        elif node.kind is LibraryNodeKind.EPISODE and node.parent_id is not None:
            episodes_by_parent[node.parent_id].append(node)

    series_out: list[TreeSeries] = []
    for series in sorted(series_nodes, key=lambda n: (n.title, n.id)):
        seasons_out: list[TreeSeason] = []
        for season in sorted(seasons_by_parent[series.id], key=lambda n: n.season_number or 0):
            episodes_out: list[TreeEpisode] = []
            for episode in sorted(
                episodes_by_parent[season.id], key=lambda n: n.episode_number or 0
            ):
                assert episode.sonarr_episode_id is not None, (
                    f"library node {episode.id} has kind EPISODE but sonarr_episode_id is NULL "
                    "-- this is a data-integrity violation, not a legitimate missing id"
                )
                assert episode.season_number is not None, (
                    f"library node {episode.id} has kind EPISODE but season_number is NULL "
                    "-- this is a data-integrity violation, not a legitimate missing id"
                )
                assert episode.episode_number is not None, (
                    f"library node {episode.id} has kind EPISODE but episode_number is NULL "
                    "-- this is a data-integrity violation, not a legitimate missing id"
                )
                episode_media = media_by_episode_id.get(episode.sonarr_episode_id)
                episodes_out.append(
                    TreeEpisode(
                        id=episode.id,
                        sonarr_episode_id=episode.sonarr_episode_id,
                        season_number=episode.season_number,
                        episode_number=episode.episode_number,
                        title=episode.title,
                        has_file=episode.has_file,
                        tracked=resolve_tracked(episode, nodes_by_id, default_tracked),
                        current_default_track=_adapt_default_track(episode_media),
                        file_id=episode_media.id if episode_media is not None else None,
                    )
                )
            assert season.season_number is not None, (
                f"library node {season.id} has kind SEASON but season_number is NULL "
                "-- this is a data-integrity violation, not a legitimate missing id"
            )
            seasons_out.append(
                TreeSeason(
                    id=season.id,
                    season_number=season.season_number,
                    tracked=resolve_tracked(season, nodes_by_id, default_tracked),
                    episodes=tuple(episodes_out),
                )
            )
        assert series.sonarr_series_id is not None, (
            f"library node {series.id} has kind SERIES but sonarr_series_id is NULL "
            "-- this is a data-integrity violation, not a legitimate missing id"
        )
        series_out.append(
            TreeSeries(
                id=series.id,
                sonarr_series_id=series.sonarr_series_id,
                title=series.title,
                tracked=resolve_tracked(series, nodes_by_id, default_tracked),
                seasons=tuple(seasons_out),
            )
        )

    return LibraryTree(instance_id=instance_id, series=tuple(series_out))


def build_movie_tree(session: Session, instance_id: int) -> MovieLibraryTree:
    """Build the visible, flat Movie list with resolved Tracked values (COL-99).

    Mirrors :func:`build_tree`'s hidden-node exclusion, Tracked resolution, and
    current-default-track enrichment (COL-154), but flat: a Movie node has no
    ancestor level, so its resolved Tracked value is simply its own override or
    the global default. Movies are ordered by title.
    """
    # Local import: see the TYPE_CHECKING note above -- module-scope would be circular.
    from collapsarr.media.service import list_tracked_media_by_instance

    default_tracked = get_global_settings(session).default_tracked
    nodes = list_nodes(session, instance_id)
    nodes_by_id = {node.id: node for node in nodes}

    media_by_movie_id = {
        media.radarr_movie_id: media
        for media in list_tracked_media_by_instance(session, instance_id)
        if media.radarr_movie_id is not None
    }

    movie_nodes = [n for n in nodes if n.kind is LibraryNodeKind.MOVIE and not n.hidden]

    movies_out: list[TreeMovie] = []
    for movie in sorted(movie_nodes, key=lambda n: (n.title, n.id)):
        assert movie.radarr_movie_id is not None, (
            f"library node {movie.id} has kind MOVIE but radarr_movie_id is NULL "
            "-- this is a data-integrity violation, not a legitimate missing id"
        )
        movie_media = media_by_movie_id.get(movie.radarr_movie_id)
        movies_out.append(
            TreeMovie(
                id=movie.id,
                radarr_movie_id=movie.radarr_movie_id,
                title=movie.title,
                has_file=movie.has_file,
                tracked=resolve_tracked(movie, nodes_by_id, default_tracked),
                current_default_track=_adapt_default_track(movie_media),
                file_id=movie_media.id if movie_media is not None else None,
            )
        )

    return MovieLibraryTree(instance_id=instance_id, movies=tuple(movies_out))
