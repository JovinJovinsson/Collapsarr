"""Read-only HTTP endpoint for an instance's Library tree (COL-98).

Thin GET layer over :mod:`collapsarr.library.service`, exposed as a FastAPI
:class:`~fastapi.APIRouter` mounted under ``/api`` by
:func:`collapsarr.main.create_app`. Everything under ``/api`` is gated by the
API-key middleware, so this route inherits key-based auth with no per-route
wiring.

``GET /api/library/instances/{instance_id}/tree`` returns the Sonarr
Series > Season > Episode tree for a configured instance, each node carrying its
*resolved* Tracked value (:func:`collapsarr.library.service.resolve_tracked`).
Soft-hidden nodes are omitted. A request for an unconfigured instance is a
``404``; this slice is Sonarr-only (Radarr is a later ticket).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..arr.models import InstanceType
from ..arr.service import get_instance
from ..database import get_session
from .service import build_tree

router = APIRouter(prefix="/api", tags=["library"])


# --- schemas -----------------------------------------------------------------


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
    """The full Series > Season > Episode tree for one instance's Library."""

    instance_id: int
    series: list[SeriesNode]


# --- endpoints ---------------------------------------------------------------


@router.get(
    "/library/instances/{instance_id}/tree",
    response_model=LibraryTreeResponse,
)
def get_library_tree_endpoint(
    instance_id: int, session: Session = Depends(get_session)
) -> LibraryTreeResponse:
    """Return an instance's Sonarr Library tree with resolved Tracked values.

    ``404`` if no instance with ``instance_id`` exists, or if it is not a Sonarr
    instance (Radarr library support is a later ticket).
    """
    instance = get_instance(session, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"No arr instance with id={instance_id}")
    if instance.type is not InstanceType.SONARR:
        raise HTTPException(
            status_code=404,
            detail=f"Library tree is Sonarr-only; instance id={instance_id} is {instance.type}",
        )

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
