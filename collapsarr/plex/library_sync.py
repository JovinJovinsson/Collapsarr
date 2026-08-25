"""Plex Library Item mapping table: wholesale rebuild + ratingKey resolution (COL-210).

Two responsibilities, both over the :class:`~collapsarr.plex.models.PlexLibraryItem`
table (one row per known on-disk file path -> Plex ``ratingKey`` + section):

- :func:`rebuild_library_items` -- the **full Plex Sync**: walk every configured
  library section (:func:`~collapsarr.plex.client.list_library_sections`), list
  each section's file-bearing items
  (:func:`~collapsarr.plex.client.list_section_items`), and rebuild the whole
  mapping table by file path -- delete every existing row, insert one per
  ``(file_path, ratingKey, section)`` walked. Pure derived state, so a wholesale
  delete-and-reinsert is the entire update model (there is nothing operator-owned
  to preserve, unlike :func:`collapsarr.library.service.sync_library`'s
  soft-hide). Driven on a weekly cadence / on save / on demand by
  :class:`collapsarr.plex.scheduler.PlexSyncScheduler`.

- :func:`resolve_rating_key` -- given a file path, return its Plex ``ratingKey``:
  the mapping table first, then -- on a miss -- a **single** live scoped Plex
  query (:func:`~collapsarr.plex.client.search_items`, scoped by the file's known
  title/season/episode via the Sonarr/Radarr -> Library Node bridge), then
  **give up silently** if that also fails. This is a soft-fail path by design:
  it never raises and never logs at a level that would alarm an operator -- a
  file with no Plex counterpart is an ordinary, expected outcome (e.g. media
  Plex hasn't scanned yet), not an error. A live-query **hit** is written back
  into the mapping table immediately (COL-245) -- rather than left for the next
  scheduled/on-demand Plex Sync (:func:`rebuild_library_items`) to pick up --
  so a caller resolving the same path again (e.g. a retried Job) gets a table
  hit next time. That write-back runs in its own inner try/except
  (:func:`_persist_live_hit`): a persistence hiccup must never turn an
  otherwise-successful resolution into a give-up, so the caller still gets
  the resolved ``ratingKey`` back even if the cache write itself fails.

The client calls are injectable seams (``list_sections``/``list_items``/
``search``), defaulting to the real client functions -- mirroring
:class:`collapsarr.jobs.scheduler.JobScheduler`'s ``catalog_fetch`` seam, so the
sync/resolution logic can be unit-tested with in-memory fakes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from collapsarr.library.models import LibraryNode, LibraryNodeKind, make_node_key
from collapsarr.library.service import get_node_by_source_id
from collapsarr.media.service import get_tracked_media

from .client import (
    PLEX_EPISODE_TYPE,
    PlexMediaItem,
    SectionItemsResult,
    SectionsResult,
    list_library_sections,
    list_section_items,
    search_items,
)
from .models import PlexLibraryItem

logger = logging.getLogger(__name__)

#: Plex section ``type`` that holds TV episodes -- its ``/all`` needs the
#: episode ``type`` filter to reach the file-bearing episodes (see
#: :func:`~collapsarr.plex.client.list_section_items`). Any other section type
#: (``"movie"`` and, defensively, anything else) lists file-bearing items
#: directly with no filter.
_SHOW_SECTION_TYPE = "show"

#: Injectable client-call seams, defaulting to the real
#: :mod:`collapsarr.plex.client` functions (see the module docstring).
ListSectionsFn = Callable[..., SectionsResult]
ListItemsFn = Callable[..., SectionItemsResult]
SearchFn = Callable[..., SectionItemsResult]


def rebuild_library_items(
    session: Session,
    *,
    base_url: str,
    token: str,
    transport: object | None = None,
    list_sections: ListSectionsFn = list_library_sections,
    list_items: ListItemsFn = list_section_items,
) -> int:
    """Walk every Plex section and rebuild the mapping table by file path (COL-210).

    Returns the number of ``(file_path -> ratingKey)`` rows the rebuilt table
    now holds. Three cases leave the **existing table untouched** (a no-op,
    returning ``0``) rather than replacing it -- the rebuild only ever commits
    once it has genuine confidence in what it gathered, so a transient outage
    never wipes a good map (mirroring :meth:`collapsarr.jobs.scheduler.
    JobScheduler._sync_instance_library`, which never soft-hides on a failed
    catalog fetch):

    1. a blank ``base_url`` (Plex not configured);
    2. a failed section *listing* call; or
    3. **every** configured section's item-fetch failing (distinct from a
       library that is genuinely configured with zero sections, or whose
       sections legitimately report zero items -- both of those *do* commit an
       empty table, since that's a truthful rebuild, not a fetch failure).

    A *partial* failure -- some sections list successfully, others don't --
    still commits: the successful sections' items replace the table, and each
    failed section is logged and skipped, so that section's items simply won't
    appear in the new map until the next successful sync (one unreachable/
    erroring section doesn't block the others' otherwise-good data from
    landing).

    For each section, ``item_type`` is the episode filter for a ``show``
    section (so ``/all`` returns file-bearing episodes, not show/season
    containers) and unset otherwise. Every file path Plex reports for an item
    (a single item can have several parts) maps to that item's ``ratingKey``;
    duplicate paths resolve last-writer-wins.
    """
    if not base_url:
        return 0

    sections_result = list_sections(base_url, token, transport=transport)
    if not sections_result.ok:
        logger.warning(
            "Plex sync: could not list library sections (%s); leaving the mapping "
            "table unchanged",
            sections_result.error,
        )
        return 0

    mapping: dict[str, tuple[str, str]] = {}  # file_path -> (rating_key, section_key)
    sections_succeeded = 0
    for section in sections_result.sections:
        item_type = PLEX_EPISODE_TYPE if section.type == _SHOW_SECTION_TYPE else None
        items_result = list_items(
            base_url, token, section.key, item_type=item_type, transport=transport
        )
        if not items_result.ok:
            logger.warning(
                "Plex sync: could not list items in section %r (%s); skipping it",
                section.key,
                items_result.error,
            )
            continue
        sections_succeeded += 1
        for item in items_result.items:
            for file_path in item.file_paths:
                mapping[file_path] = (item.rating_key, item.section_key or section.key)

    # Every configured section failed its item-fetch: an empty `mapping` here
    # would be a fetch-failure artefact, not a truthful "the library is empty"
    # rebuild -- committing it would silently destroy a previously-good map on
    # what may be a purely transient outage. A library with zero sections
    # configured at all (`sections_result.sections` itself empty) is not this
    # case -- there was nothing to fail, so an empty rebuild there is truthful
    # and proceeds below.
    if sections_result.sections and sections_succeeded == 0:
        logger.warning(
            "Plex sync: every configured library section failed to list its "
            "items; leaving the mapping table unchanged"
        )
        return 0

    session.execute(delete(PlexLibraryItem))
    session.add_all(
        PlexLibraryItem(file_path=file_path, rating_key=rating_key, section_key=section_key)
        for file_path, (rating_key, section_key) in mapping.items()
    )
    session.commit()
    return len(mapping)


@dataclass(frozen=True, slots=True)
class _LiveQueryScope:
    """The title/season/episode a live fallback query is scoped by (from the Library node)."""

    title: str
    season_number: int | None
    episode_number: int | None
    is_episode: bool


def resolve_rating_key(
    session: Session,
    file_path: str | Path,
    *,
    base_url: str,
    token: str,
    transport: object | None = None,
    search: SearchFn = search_items,
) -> str | None:
    """Resolve ``file_path``'s Plex ``ratingKey``: mapping table, then live query, then give up.

    1. **Mapping table** (:func:`rebuild_library_items`'s output): an indexed
       lookup by exact ``file_path``. A hit returns immediately.
    2. **Single live scoped query** on a miss: bridge the file to its
       :class:`~collapsarr.library.models.LibraryNode` (via its tracked-media
       row's Arr ids) to learn its title (and season/episode for a TV episode),
       issue one :func:`~collapsarr.plex.client.search_items` query scoped by
       that title, and pick the candidate matching the season/episode (or the
       first movie candidate). A match returns its ``ratingKey``.
    3. **Give up silently** otherwise: a blank ``base_url``, an un-bridgeable
       file, a failed/empty query, or no matching candidate all return ``None``
       with no error surfaced. Any unexpected exception -- including one raised
       by the mapping-table lookup itself (step 1), not just the live-query
       fallback -- is swallowed too (logged at debug only): the whole path,
       start to finish, is soft-fail by design, so a DB hiccup on the cheap
       indexed lookup gives up exactly like a failed live query would, rather
       than propagating.

    A live-query **hit** (step 2 succeeding) is written back into the mapping
    table immediately, via :func:`_persist_live_hit` -- see the module
    docstring for why that write is isolated from this function's own
    give-up-on-any-exception behaviour.
    """
    path_str = str(file_path)

    try:
        existing = session.scalars(
            select(PlexLibraryItem).where(PlexLibraryItem.file_path == path_str)
        ).one_or_none()
        if existing is not None:
            return existing.rating_key

        if not base_url:
            return None

        scope = _resolve_scope(session, path_str)
        if scope is None:
            return None
        result = search(base_url, token, scope.title, transport=transport)
        if not result.ok:
            return None
        matched = _match_item(result, scope)
        if matched is None:
            return None

        _persist_live_hit(session, path_str, matched)
        return matched.rating_key
    except Exception:  # noqa: BLE001 - soft-fail path: never propagate, never alarm
        logger.debug("Plex ratingKey resolution failed for %s; giving up", path_str, exc_info=True)
        return None


def _persist_live_hit(session: Session, file_path: str, item: PlexMediaItem) -> None:
    """Write a live-query ratingKey hit into the mapping table immediately (COL-245).

    Runs inside its own try/except, deliberately separate from
    :func:`resolve_rating_key`'s own outer one: a persistence hiccup here (a
    DB error, a race with a concurrent Plex Sync) must never turn an
    otherwise-successful live resolution into a give-up -- the caller already
    has a good ``ratingKey`` to return regardless of whether this write lands,
    so any failure here is logged at debug and swallowed, not propagated.

    Upserts by ``file_path``: :func:`resolve_rating_key` only reaches this
    point once it has already confirmed no row exists for the path, so a
    plain insert is the expected case; the fallback update path covers the
    narrow race where a concurrent write (e.g. a Plex Sync run) landed a row
    for the same path in between.
    """
    try:
        existing = session.scalars(
            select(PlexLibraryItem).where(PlexLibraryItem.file_path == file_path)
        ).one_or_none()
        if existing is not None:
            existing.rating_key = item.rating_key
            existing.section_key = item.section_key or existing.section_key
        else:
            session.add(
                PlexLibraryItem(
                    file_path=file_path,
                    rating_key=item.rating_key,
                    section_key=item.section_key or "",
                )
            )
        session.commit()
    except Exception:  # noqa: BLE001 - never let a cache-write hiccup lose a good resolution
        session.rollback()
        logger.debug(
            "Plex ratingKey live-query hit for %s could not be cached", file_path, exc_info=True
        )


def _resolve_scope(session: Session, file_path: str) -> _LiveQueryScope | None:
    """Bridge a file path to the title/season/episode a live query should be scoped by.

    Uses the same Sonarr/Radarr -> Library Node bridge the Tracked gate uses
    (:func:`~collapsarr.library.service.get_node_by_source_id`), via the file's
    :class:`~collapsarr.media.models.TrackedMediaFile` row. Returns ``None``
    when the file isn't tracked, was never bridged to an instance, or has no
    resolvable node -- all of which mean there is nothing to scope a query by.
    For an episode, the scope's title is the *series* title (Plex's search is by
    show), read from the sibling Series node.
    """
    media = get_tracked_media(session, file_path)
    if media is None or media.instance_id is None:
        return None
    node = get_node_by_source_id(
        session,
        instance_id=media.instance_id,
        sonarr_episode_id=media.sonarr_episode_id,
        radarr_movie_id=media.radarr_movie_id,
    )
    if node is None:
        return None
    if node.kind is LibraryNodeKind.MOVIE:
        return _LiveQueryScope(
            title=node.title, season_number=None, episode_number=None, is_episode=False
        )
    if node.kind is LibraryNodeKind.EPISODE:
        title = _series_title(session, media.instance_id, node) or node.title
        return _LiveQueryScope(
            title=title,
            season_number=node.season_number,
            episode_number=node.episode_number,
            is_episode=True,
        )
    return None


def _series_title(session: Session, instance_id: int, episode_node: LibraryNode) -> str | None:
    """The Series title for an Episode node, from its sibling Series node (or ``None``)."""
    if episode_node.sonarr_series_id is None:
        return None
    series_node = session.scalars(
        select(LibraryNode).where(
            LibraryNode.instance_id == instance_id,
            LibraryNode.node_key
            == make_node_key(LibraryNodeKind.SERIES, series_id=episode_node.sonarr_series_id),
        )
    ).one_or_none()
    return series_node.title if series_node is not None else None


def _match_item(result: SectionItemsResult, scope: _LiveQueryScope) -> PlexMediaItem | None:
    """Pick the item from a search result that matches ``scope``.

    For an episode, the candidate must be a Plex ``episode`` whose
    ``season_number``/``episode_number`` both match the scope. For a movie, the
    first ``movie`` candidate wins (the query was already scoped by its title);
    if none is explicitly typed ``movie``, the first candidate is taken.
    Returns the full :class:`~collapsarr.plex.client.PlexMediaItem` (not just
    its ``rating_key``) -- :func:`_persist_live_hit` also needs its
    ``section_key`` for the mapping-table write-back.
    """
    if scope.is_episode:
        for item in result.items:
            if (
                item.type == "episode"
                and item.season_number == scope.season_number
                and item.episode_number == scope.episode_number
            ):
                return item
        return None
    for item in result.items:
        if item.type == "movie":
            return item
    return result.items[0] if result.items else None
