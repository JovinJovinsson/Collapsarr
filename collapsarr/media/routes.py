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

A third endpoint, ``GET /api/files/{file_id}/audio-streams`` (COL-204),
reuses the same id lookup to report the file's *current, live* audio-stream
layout -- the same :func:`~collapsarr.downmix.probe.probe_audio_streams`
call the downmix/default-audio pipelines use, run fresh on every request
(never cached or stored), so the response always reflects the file's actual
state on disk right now, including whichever stream currently carries the
Default Audio Track disposition. An unprobeable file (missing on disk,
corrupt, ffprobe unavailable) degrades to ``probeable=False`` with an
``error`` message rather than raising -- this endpoint still 404s on an
unknown ``file_id``, but a *known* file that merely can't be probed right
now is a ``200`` with an empty/unprobeable payload, not a failed request.

A fourth endpoint, ``GET /api/files/{file_id}/poster`` (COL-205), returns
poster metadata for a file. No Plex integration exists yet, so it always
responds with the placeholder state (``status="placeholder"``,
``poster_url=None``) for a ``file_id`` that resolves to a tracked file --
this is a deliberately stable contract Phase 2 (COL-212) will satisfy by
resolving a real Plex poster URL without changing the response shape, so the
frontend never needs to change how it reads this endpoint. Like the other
two, it raises ``404`` only when ``file_id`` itself doesn't resolve -- a
*missing poster* is the normal state in this phase, not an error, and is
never represented as one.

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
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_session
from ..downmix.probe import AudioStreamInfo, FfprobeError, probe_audio_streams
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


class AudioStreamOut(BaseModel):
    """One of a file's current audio streams, as live-probed via ffprobe (COL-204)."""

    index: int
    codec: str
    channels: int
    channel_layout: str
    language: str
    #: Whether this stream currently carries the container's Default Audio
    #: Track disposition (ffprobe's ``disposition.default``).
    is_default: bool

    @classmethod
    def from_probe(cls, stream: AudioStreamInfo) -> AudioStreamOut:
        return cls(
            index=stream.index,
            codec=stream.codec,
            channels=stream.channels,
            channel_layout=stream.channel_layout,
            language=stream.language,
            is_default=stream.is_default,
        )


class AudioStreamsResponse(BaseModel):
    """Response for ``GET /api/files/{file_id}/audio-streams`` (COL-204).

    ``probeable`` is ``False`` when the file couldn't be probed right now
    (missing on disk, corrupt, ffprobe unavailable/timed out) -- ``streams``
    is then always empty and ``error`` carries a human-readable reason, so
    the file detail page can render a graceful "unavailable" message instead
    of a broken table. ``probeable=True`` always reflects the file's actual
    current state: this is never cached or stored, so a request made right
    after a downmix/Set-Default-Audio job completes sees the *new* layout.
    """

    probeable: bool
    error: str | None = None
    streams: list[AudioStreamOut] = []


class FilePosterResponse(BaseModel):
    """Poster metadata for a tracked file (COL-205).

    No Plex integration exists yet, so ``status`` is always
    ``"placeholder"`` and ``poster_url`` is always ``None`` in this phase --
    this shape is a deliberately stable contract: Phase 2 (COL-212) resolves
    a real Plex poster URL by populating ``poster_url`` and flipping
    ``status`` to ``"available"``, without changing this shape or requiring
    any frontend change to consume it.
    """

    file_id: int
    status: Literal["available", "placeholder"]
    poster_url: str | None = None


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


@router.get("/files/{file_id}/audio-streams", response_model=AudioStreamsResponse)
def get_file_audio_streams_endpoint(
    file_id: int, session: Session = Depends(get_session)
) -> AudioStreamsResponse:
    """Return ``file_id``'s current audio streams, live-probed via ffprobe (COL-204).

    Shares :func:`~collapsarr.downmix.probe.probe_audio_streams` with the
    downmix/default-audio pipelines -- the same probe, run fresh on every
    call, never cached or stored, so the response always matches the file's
    actual state on disk right now. Raises ``404`` when ``file_id`` was never
    valid or no longer exists (same as ``GET /api/files/{file_id}``); a
    *known* file that can't currently be probed (missing on disk, corrupt,
    ffprobe unavailable/timed out) degrades to a ``200`` with
    ``probeable=False`` instead, matching how the manual-trigger endpoints
    treat an unprobeable file as a reportable outcome rather than a hard
    failure.
    """
    media = get_tracked_media_by_id(session, file_id)
    if media is None:
        raise HTTPException(status_code=404, detail=f"No tracked file with id={file_id}.")

    try:
        streams = probe_audio_streams(media.file_path)
    except FfprobeError as exc:
        return AudioStreamsResponse(probeable=False, error=str(exc), streams=[])

    return AudioStreamsResponse(
        probeable=True,
        streams=[AudioStreamOut.from_probe(stream) for stream in streams],
    )


@router.get("/files/{file_id}/poster", response_model=FilePosterResponse)
def get_file_poster_endpoint(
    file_id: int, session: Session = Depends(get_session)
) -> FilePosterResponse:
    """Return poster metadata for a tracked file (COL-205).

    No Plex integration exists yet, so this always returns the placeholder
    state (``status="placeholder"``, ``poster_url=None``) for a ``file_id``
    that resolves to a tracked file -- see the module docstring and
    :class:`FilePosterResponse` for why this shape is deliberately stable
    across the Phase 2 (COL-212) swap. Raises ``404`` only when ``file_id``
    itself was never valid or no longer exists, matching
    ``GET /api/files/{file_id}`` (COL-203) -- a missing *poster* is not an
    error condition in this phase and is never surfaced as one.
    """
    media = get_tracked_media_by_id(session, file_id)
    if media is None:
        raise HTTPException(status_code=404, detail=f"No tracked file with id={file_id}.")

    return FilePosterResponse(file_id=media.id, status="placeholder", poster_url=None)
