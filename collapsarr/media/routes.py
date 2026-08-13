"""HTTP REST endpoints for the wanted-list and single-file lookup (COL-28, COL-203).

Thin GET layer over :mod:`collapsarr.media.service`, exposed as a FastAPI
:class:`~fastapi.APIRouter` mounted under ``/api`` by
:func:`collapsarr.main.create_app`. Everything under ``/api`` is gated by the
API-key middleware (COL-26), so these routes inherit key-based auth with no
per-route wiring.

Two GET endpoints share the same ``WantedFile`` response shape:

- ``GET /api/wanted`` -- every **Tracked** file missing at least one
  currently-enabled target (COL-28), the Wanted view's data source.
- ``GET /api/files/{file_id}`` (COL-203) -- a single file by id, regardless
  of whether it currently has any missing targets or is Tracked. This is
  what the file detail page (``FileDetailPage``) reads from, since a
  fully-processed file (zero missing targets) is deliberately excluded from
  ``GET /api/wanted`` but must still be viewable by id. Returns ``404`` when
  ``file_id`` was never valid or no longer exists.

The "wanted-list" is every tracked file missing at least one *currently
enabled* target -- the same notion Sonarr/Radarr's ``/wanted/missing`` view
expresses. Which targets count as enabled is read live from the persisted
:class:`~collapsarr.settings.models.GlobalSettings` row rather than re-derived
from stored status rows, so a settings change is reflected immediately (a
target no longer enabled stops appearing as "wanted" without every file needing
a rescan first). Each returned file carries the exact ``(language, target)``
pairs still missing, which is the granularity the downmix job queue and a
future Wanted UI both need.

Each file also carries its resolved **Tracked** value (COL-101), by bridging
its ``instance_id``/``sonarr_episode_id``/``radarr_movie_id``
(:mod:`collapsarr.media.models`, populated from a scan/webhook event -- see
:func:`~collapsarr.media.service.upsert_tracked_media`) to the matching
:class:`~collapsarr.library.models.LibraryNode`
(:func:`~collapsarr.library.service.get_node_by_source_id`) and resolving it
(:func:`~collapsarr.library.service.resolve_tracked`) the same way the
Library tree endpoint does. ``library_node_id``/``node_type``/``tracked`` are
all ``None`` when the bridge can't resolve (no id captured yet, or the id no
longer matches any node) -- ``FileDetailPage`` shows a "status unavailable"
message rather than a broken toggle in that case, since ``library_node_id``
is exactly the ``node_id`` reference the row toggle
(``POST /api/library/tracked``) needs.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_session
from ..downmix.targets import DownmixTarget
from ..library.models import LibraryNode
from ..library.service import get_node_by_source_id, list_nodes, resolve_tracked
from ..settings.service import as_downmix_settings, get_global_settings
from .models import MediaTargetStatus, TrackedMediaFile
from .service import get_tracked_media_by_id, list_files_missing_targets, list_target_statuses

router = APIRouter(prefix="/api", tags=["wanted"])


# --- schemas -----------------------------------------------------------------


class WantedTarget(BaseModel):
    """One ``(language, target)`` pair still missing on a wanted file."""

    language: str
    target: DownmixTarget


class WantedFile(BaseModel):
    """A tracked file missing at least one enabled target, with those pairs."""

    id: int
    file_path: str
    missing_targets: list[WantedTarget]
    created_at: datetime
    updated_at: datetime
    #: The bridged Library node's id (COL-101) -- the ``node_id`` a Tracked
    #: toggle on this file passes to ``POST /api/library/tracked``. ``None``
    #: when the bridge hasn't resolved (see module docstring).
    library_node_id: int | None = None
    #: The bridged node's kind, always ``"episode"`` or ``"movie"`` when set
    #: (a Series/Season never owns a tracked-media file directly).
    node_type: str | None = None
    #: The bridged node's *resolved* Tracked value (ancestor-override
    #: resolution included), or ``None`` when the bridge hasn't resolved.
    tracked: bool | None = None


# --- endpoints ---------------------------------------------------------------


def _resolve_library_tracked(
    session: Session, media: TrackedMediaFile, *, nodes_cache: dict[int, dict[int, LibraryNode]]
) -> tuple[int | None, str | None, bool | None]:
    """Bridge ``media`` to its Library node and resolve its Tracked value (COL-101).

    Returns ``(library_node_id, node_type, tracked)``, all ``None`` together
    when the bridge doesn't resolve (``media.instance_id`` unset, no matching
    node found, or the id fields were never captured). ``nodes_cache`` is the
    caller's per-``instance_id`` full node set (:func:`~collapsarr.library.service.list_nodes`,
    needed for :func:`~collapsarr.library.service.resolve_tracked`'s ancestor
    walk) -- populated lazily here and reused across every file from the same
    instance in one ``list_wanted_endpoint`` call, so a Wanted list with many
    files from one instance doesn't re-fetch that instance's whole node set
    per file.
    """
    if media.instance_id is None:
        return None, None, None

    node = get_node_by_source_id(
        session,
        instance_id=media.instance_id,
        sonarr_episode_id=media.sonarr_episode_id,
        radarr_movie_id=media.radarr_movie_id,
    )
    if node is None:
        return None, None, None

    nodes_by_id = nodes_cache.get(media.instance_id)
    if nodes_by_id is None:
        nodes_by_id = {n.id: n for n in list_nodes(session, media.instance_id)}
        nodes_cache[media.instance_id] = nodes_by_id

    default_tracked = get_global_settings(session).default_tracked
    tracked = resolve_tracked(node, nodes_by_id, default_tracked)
    return node.id, node.kind.value, tracked


def _to_wanted_file(
    session: Session,
    media: TrackedMediaFile,
    *,
    enabled_targets: frozenset[DownmixTarget],
    nodes_cache: dict[int, dict[int, LibraryNode]],
) -> WantedFile:
    """Build a :class:`WantedFile` response row for ``media``.

    Shared by both ``GET /api/wanted`` and ``GET /api/files/{file_id}``
    (COL-203) -- the two endpoints return the identical shape, differing only
    in which files they select (a Wanted-membership query vs. a single id
    lookup independent of Wanted-queue membership).
    """
    missing = [
        WantedTarget(language=status.language, target=status.target)
        for status in list_target_statuses(session, media.file_path)
        if status.status == MediaTargetStatus.MISSING and status.target in enabled_targets
    ]
    library_node_id, node_type, tracked = _resolve_library_tracked(
        session, media, nodes_cache=nodes_cache
    )
    return WantedFile(
        id=media.id,
        file_path=media.file_path,
        missing_targets=missing,
        created_at=media.created_at,
        updated_at=media.updated_at,
        library_node_id=library_node_id,
        node_type=node_type,
        tracked=tracked,
    )


@router.get("/wanted", response_model=list[WantedFile])
def list_wanted_endpoint(session: Session = Depends(get_session)) -> list[WantedFile]:
    """List tracked files missing at least one currently-enabled downmix target.

    Enabled targets are read from the global settings row; each file's still
    ``missing`` ``(language, target)`` pairs (restricted to those enabled
    targets) are attached. Ordered by file id (insertion order), matching the
    service layer. Each file also carries its resolved Tracked value bridged
    from its Library node, when resolvable (COL-101 -- see module docstring).
    """
    enabled_targets = as_downmix_settings(get_global_settings(session)).enabled_targets
    files = list_files_missing_targets(session, enabled_targets=enabled_targets)

    nodes_cache: dict[int, dict[int, LibraryNode]] = {}
    return [
        _to_wanted_file(session, media, enabled_targets=enabled_targets, nodes_cache=nodes_cache)
        for media in files
    ]


@router.get("/files/{file_id}", response_model=WantedFile)
def get_file_endpoint(file_id: int, session: Session = Depends(get_session)) -> WantedFile:
    """Return a single tracked file by id, independent of Wanted-queue membership (COL-203).

    Unlike ``GET /api/wanted``, this doesn't filter on whether the file has
    any currently-missing targets -- a fully-processed file (every enabled
    target already present) still resolves here with an empty
    ``missing_targets`` list, since the file detail page needs to render it
    too. Raises ``404`` when ``file_id`` was never valid or no longer exists.
    """
    media = get_tracked_media_by_id(session, file_id)
    if media is None:
        raise HTTPException(status_code=404, detail=f"No tracked file with id={file_id}.")

    enabled_targets = as_downmix_settings(get_global_settings(session)).enabled_targets
    nodes_cache: dict[int, dict[int, LibraryNode]] = {}
    return _to_wanted_file(session, media, enabled_targets=enabled_targets, nodes_cache=nodes_cache)
