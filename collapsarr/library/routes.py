"""HTTP endpoints for an instance's Library tree and Tracked writes (COL-98/COL-99/COL-101).

Thin layer over :mod:`collapsarr.library.service`, exposed as a FastAPI
:class:`~fastapi.APIRouter` mounted under ``/api`` by
:func:`collapsarr.main.create_app`. Everything under ``/api`` is gated by the
API-key middleware, so these routes inherit key-based auth with no per-route
wiring.

``GET /api/library/instances/{instance_id}/tree`` returns, for a configured
Sonarr instance, the Series > Season > Episode tree; for a configured Radarr
instance (COL-99), the flat Movie list. Each node carries its *resolved*
Tracked value (:func:`collapsarr.library.service.resolve_tracked`). Soft-hidden
nodes are omitted. A request for an unconfigured instance is a ``404``.

``POST /api/library/tracked`` (COL-101) is the write path: it accepts one or
more ``{node_type, node_id}`` references and a target Tracked value, and
applies :func:`collapsarr.library.service.set_tracked` -- with its existing
cascade-to-descendants rule for a Series/Season reference -- to each one. This
is the single-item Tracked toggle's endpoint (a bulk request of one reference)
as well as the future multi-select bulk action's (COL-103).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..arr.models import InstanceType
from ..arr.service import get_instance
from ..database import get_session
from ..media.models import TrackedMediaFile
from ..media.service import list_tracked_media_by_instance
from .models import LibraryNode, LibraryNodeKind
from .service import LibraryNodeNotFoundError, build_movie_tree, build_tree, get_node, set_tracked

router = APIRouter(prefix="/api", tags=["library"])


# --- schemas -----------------------------------------------------------------


class CurrentDefaultTrack(BaseModel):
    """A file-bearing leaf node's current Default Audio Track snapshot (COL-154).

    Mirrors :class:`~collapsarr.media.models.TrackedMediaFile`'s
    ``current_default_language``/``current_default_channel_layout`` columns,
    refreshed at every existing probe call site (scan, webhook import, manual
    trigger). Rendered by the Library page as e.g. "Danish · 5.1".
    """

    language: str
    channel_layout: str


class EpisodeNode(BaseModel):
    """An Episode node with its resolved Tracked value."""

    id: int
    kind: str = "episode"
    sonarr_episode_id: int
    season_number: int
    episode_number: int
    title: str
    has_file: bool
    tracked: bool
    #: ``None`` when the file hasn't been probed since COL-154 shipped, or
    #: its ffprobe metadata carries no Default Audio Track disposition flag
    #: on any stream -- both render as "unknown" on the Library page.
    current_default_track: CurrentDefaultTrack | None = None


class SeasonNode(BaseModel):
    """A Season node with its resolved Tracked value and its episodes."""

    id: int
    kind: str = "season"
    season_number: int
    tracked: bool
    episodes: list[EpisodeNode]


class SeriesNode(BaseModel):
    """A Series node with its resolved Tracked value and its seasons."""

    id: int
    kind: str = "series"
    sonarr_series_id: int
    title: str
    tracked: bool
    seasons: list[SeasonNode]


class LibraryTreeResponse(BaseModel):
    """The full Series > Season > Episode tree for one Sonarr instance's Library."""

    instance_id: int
    series: list[SeriesNode]


class MovieNode(BaseModel):
    """A Movie node with its resolved Tracked value (COL-99)."""

    id: int
    kind: str = "movie"
    radarr_movie_id: int
    title: str
    has_file: bool
    tracked: bool
    #: See :attr:`EpisodeNode.current_default_track` (COL-154).
    current_default_track: CurrentDefaultTrack | None = None


class MovieLibraryTreeResponse(BaseModel):
    """The full, flat Movie list for one Radarr instance's Library (COL-99)."""

    instance_id: int
    movies: list[MovieNode]


class TrackedNodeReference(BaseModel):
    """One ``{node_type, node_id}`` reference in a bulk Tracked-update request (COL-101).

    ``node_type`` must match the referenced node's actual
    :class:`~collapsarr.library.models.LibraryNodeKind` -- a client-side
    mismatch (e.g. calling an Episode id a ``"series"``) is a ``422``, not
    silently corrected, since it usually means the caller is pointing at the
    wrong node entirely.
    """

    node_type: LibraryNodeKind
    node_id: int


class BulkTrackedUpdateRequest(BaseModel):
    """Body for ``POST /api/library/tracked``: one or more references + a target value."""

    references: list[TrackedNodeReference] = Field(min_length=1)
    tracked: bool


class UpdatedTrackedNode(BaseModel):
    """One directly-referenced node's state after a Tracked update (COL-101).

    Only the *directly*-referenced nodes are reported here, not any
    descendants a Series/Season reference cascaded to -- the tree endpoint
    above (re-fetched by the caller) is the source of truth for the resulting
    full tree, cascade included.
    """

    id: int
    kind: LibraryNodeKind
    tracked: bool


class BulkTrackedUpdateResponse(BaseModel):
    """Response for ``POST /api/library/tracked``: each reference's resulting state."""

    updated: list[UpdatedTrackedNode]


# --- endpoints ---------------------------------------------------------------


def _current_default_track(media: TrackedMediaFile | None) -> CurrentDefaultTrack | None:
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
    return CurrentDefaultTrack(
        language=media.current_default_language,
        channel_layout=media.current_default_channel_layout,
    )


@router.get(
    "/library/instances/{instance_id}/tree",
    response_model=LibraryTreeResponse | MovieLibraryTreeResponse,
)
def get_library_tree_endpoint(
    instance_id: int, session: Session = Depends(get_session)
) -> LibraryTreeResponse | MovieLibraryTreeResponse:
    """Return an instance's Library tree with resolved Tracked values.

    A Sonarr instance returns the Series > Season > Episode tree
    (:class:`LibraryTreeResponse`); a Radarr instance (COL-99) returns the flat
    Movie list (:class:`MovieLibraryTreeResponse`). ``404`` if no instance with
    ``instance_id`` exists.

    Each Episode/Movie leaf also carries its current-default-track snapshot
    (COL-154), bridged from :mod:`collapsarr.media.service`'s tracked-media
    rows the same way Tracked resolution bridges the other direction: one
    bulk fetch of every tracked-media row for this instance
    (:func:`~collapsarr.media.service.list_tracked_media_by_instance`), keyed
    by the same ``sonarr_episode_id``/``radarr_movie_id`` this tree already
    carries, rather than a per-node query.
    """
    instance = get_instance(session, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"No arr instance with id={instance_id}")

    tracked_media = list_tracked_media_by_instance(session, instance_id)

    if instance.type is InstanceType.RADARR:
        media_by_movie_id = {
            media.radarr_movie_id: media
            for media in tracked_media
            if media.radarr_movie_id is not None
        }
        movie_tree = build_movie_tree(session, instance_id)
        return MovieLibraryTreeResponse(
            instance_id=movie_tree.instance_id,
            movies=[
                MovieNode(
                    id=movie.id,
                    radarr_movie_id=movie.radarr_movie_id,
                    title=movie.title,
                    has_file=movie.has_file,
                    tracked=movie.tracked,
                    current_default_track=_current_default_track(
                        media_by_movie_id.get(movie.radarr_movie_id)
                    ),
                )
                for movie in movie_tree.movies
            ],
        )

    media_by_episode_id = {
        media.sonarr_episode_id: media
        for media in tracked_media
        if media.sonarr_episode_id is not None
    }
    tree = build_tree(session, instance_id)
    return LibraryTreeResponse(
        instance_id=tree.instance_id,
        series=[
            SeriesNode(
                id=series.id,
                sonarr_series_id=series.sonarr_series_id,
                title=series.title,
                tracked=series.tracked,
                seasons=[
                    SeasonNode(
                        id=season.id,
                        season_number=season.season_number,
                        tracked=season.tracked,
                        episodes=[
                            EpisodeNode(
                                id=episode.id,
                                sonarr_episode_id=episode.sonarr_episode_id,
                                season_number=episode.season_number,
                                episode_number=episode.episode_number,
                                title=episode.title,
                                has_file=episode.has_file,
                                tracked=episode.tracked,
                                current_default_track=_current_default_track(
                                    media_by_episode_id.get(episode.sonarr_episode_id)
                                ),
                            )
                            for episode in season.episodes
                        ],
                    )
                    for season in series.seasons
                ],
            )
            for series in tree.series
        ],
    )


@router.post("/library/tracked", response_model=BulkTrackedUpdateResponse)
def bulk_update_tracked_endpoint(
    payload: BulkTrackedUpdateRequest, session: Session = Depends(get_session)
) -> BulkTrackedUpdateResponse:
    """Set Tracked on one or more Library nodes, cascading per COL-98's rule (COL-101).

    Every reference is resolved and validated *before* any write happens:
    ``404`` if any ``node_id`` doesn't exist, ``422`` if any reference's
    ``node_type`` doesn't match that node's actual kind. Once all references
    check out, :func:`~collapsarr.library.service.set_tracked` is called for
    each with ``commit=False`` -- a Series/Season reference cascades to its
    existing descendants exactly as that function already does; an
    Episode/Movie reference only ever touches itself -- and the whole batch is
    committed in a single transaction only once every reference has written
    successfully. If a reference that passed pre-validation turns out to be
    gone by the time its write is attempted (a same-request TOCTOU: e.g. it
    was deleted by a concurrent request in the gap between this endpoint's
    validation pass and its write loop), :func:`~collapsarr.library.service.
    set_tracked` raises :class:`~collapsarr.library.service.
    LibraryNodeNotFoundError`; the whole batch is rolled back rather than
    committing the references written so far, and a ``404`` is returned --
    so a bad reference, whether caught up front or mid-loop, never leaves a
    partial update.

    The response reports each *directly*-referenced node's resulting state,
    not the full cascaded set -- callers needing the resulting tree (including
    any cascaded descendants) re-fetch it from
    ``GET /library/instances/{instance_id}/tree`` above.
    """
    resolved: list[LibraryNode] = []
    for reference in payload.references:
        node = get_node(session, reference.node_id)
        if node is None:
            raise HTTPException(
                status_code=404, detail=f"No library node with id={reference.node_id}"
            )
        if node.kind is not reference.node_type:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"node_id={reference.node_id} is a {node.kind.value!r} node, "
                    f"not {reference.node_type.value!r}"
                ),
            )
        resolved.append(node)

    updated: list[UpdatedTrackedNode] = []
    try:
        for node in resolved:
            saved = set_tracked(session, node_id=node.id, tracked=payload.tracked, commit=False)
            assert saved.tracked_override is not None  # just written above
            updated.append(
                UpdatedTrackedNode(id=saved.id, kind=saved.kind, tracked=saved.tracked_override)
            )
    except LibraryNodeNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    session.commit()
    return BulkTrackedUpdateResponse(updated=updated)
