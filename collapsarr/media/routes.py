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

A fourth endpoint, ``GET /api/files/{file_id}/poster`` (COL-205, wired to a
real Plex poster by COL-212), returns poster metadata for a file. It resolves
the file's Plex ``ratingKey`` the same way the Analyze hook does (mapping
table first, then a live fallback query -- see
:func:`collapsarr.plex.resolve_rating_key`) and, on a hit, responds with
``status="available"`` and a same-origin ``poster_url`` pointing at the fifth
endpoint below. A resolution failure at *any* stage -- Plex not configured
(a blank ``base_url``/``token`` on the singleton
:class:`~collapsarr.plex.models.PlexConnection` row), a mapping-table miss
with no live-fallback match, or any other soft-fail outcome
``resolve_rating_key`` reports -- falls back to the original
``status="placeholder"``/``poster_url=None`` shape with no error surfaced;
this is still the deliberately stable contract COL-205 established, so the
frontend needs no changes to consume either outcome. Like the other three, it
raises ``404`` only when ``file_id`` itself doesn't resolve -- a *missing
poster* is a normal outcome, not an error, and is never represented as one.

A fifth endpoint, ``GET /api/files/{file_id}/poster/image`` (COL-212), is
what a returned ``poster_url`` points at: it re-resolves the same
``ratingKey`` and streams the poster's actual image bytes back from Plex
(:func:`collapsarr.plex.fetch_poster_image`), proxied entirely server-side so
the ``X-Plex-Token`` never reaches the browser. The frontend's ``<img>`` tag
hits this directly (same-origin, authenticated by the browser's session
cookie -- see ``collapsarr/auth/enforcement.py``), so no token or Plex URL is
ever present in a JSON response body. Raises ``404`` for an unknown
``file_id`` or whenever the poster can't currently be resolved/fetched --
the frontend's existing ``<img onError>`` handler already swaps to the local
placeholder graphic on any failure, so this endpoint doesn't need its own
placeholder-image fallback.

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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_session
from ..downmix.probe import AudioStreamInfo, FfprobeError, probe_audio_streams
from ..downmix.targets import DownmixTarget
from ..library.models import LibraryNode
from ..library.service import get_node_by_source_id, list_nodes, resolve_tracked
from ..plex import PlexConnection, fetch_poster_image, get_plex_connection, resolve_rating_key
from ..settings.service import as_downmix_settings, get_global_settings
from ..url_base import external_path
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
    """Poster metadata for a tracked file (COL-205, real Plex resolution since COL-212).

    ``status="available"`` with a same-origin ``poster_url`` (pointing at
    ``GET /api/files/{file_id}/poster/image``) once the file's Plex
    ``ratingKey`` resolves; ``status="placeholder"``/``poster_url=None``
    otherwise -- the same shape COL-205 established, so no frontend change is
    needed to consume either outcome.
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


def _resolve_poster_rating_key(
    session: Session, media: TrackedMediaFile, connection: PlexConnection
) -> str | None:
    """Resolve ``media``'s Plex ``ratingKey`` for the poster endpoints, or ``None``.

    Shared by both poster endpoints below, each of which fetches ``connection``
    once (:func:`collapsarr.plex.get_plex_connection`) and passes it in here,
    rather than this helper re-fetching it itself -- the image endpoint needs
    the same row's ``base_url``/``token`` again right after this call to fetch
    the actual bytes, so fetching it once per request avoids a redundant round
    trip. Returns ``None`` (soft-fail, never raises) whenever
    :attr:`~collapsarr.plex.models.PlexConnection.is_configured` is ``False``,
    so neither endpoint issues a live query against a still-blank connection;
    otherwise defers entirely to :func:`collapsarr.plex.resolve_rating_key`
    (mapping table, then a single live fallback query, then give up silently
    -- see its own docstring).
    """
    if not connection.is_configured:
        return None
    return resolve_rating_key(
        session, media.file_path, base_url=connection.base_url, token=connection.token
    )


@router.get("/files/{file_id}/poster", response_model=FilePosterResponse)
def get_file_poster_endpoint(
    request: Request, file_id: int, session: Session = Depends(get_session)
) -> FilePosterResponse:
    """Return poster metadata for a tracked file (COL-205, real Plex resolution since COL-212).

    Resolves the file's Plex ``ratingKey`` (:func:`_resolve_poster_rating_key`)
    and, on a hit, returns ``status="available"`` with ``poster_url`` pointing
    at ``GET /api/files/{file_id}/poster/image`` -- the endpoint below that
    actually streams the image bytes. ``poster_url`` is re-prefixed with the
    configured reverse-proxy ``url_base`` (:func:`collapsarr.url_base.
    external_path`, COL-117/COL-118), the same treatment a redirect
    ``Location`` header or the session cookie's path already get -- this is a
    path sent straight to the browser as an ``<img src>`` (see the module
    docstring), bypassing the frontend's own ``apiFetch``/``prefixPath``
    layer, so it has to already carry the prefix itself under a subpath
    deployment. A resolution failure at any stage (Plex not configured,
    mapping-table miss + live-fallback miss, or any other soft-fail outcome)
    falls back to ``status="placeholder"``/``poster_url=None`` with no error
    surfaced -- see the module docstring and :class:`FilePosterResponse`.
    Raises ``404`` only when ``file_id`` itself was never valid or no longer
    exists, matching ``GET /api/files/{file_id}`` (COL-203) -- a missing
    *poster* is not an error condition and is never surfaced as one.
    """
    media = get_tracked_media_by_id(session, file_id)
    if media is None:
        raise HTTPException(status_code=404, detail=f"No tracked file with id={file_id}.")

    connection = get_plex_connection(session)
    rating_key = _resolve_poster_rating_key(session, media, connection)
    if rating_key is None:
        return FilePosterResponse(file_id=media.id, status="placeholder", poster_url=None)

    url_base: str = request.app.state.settings.url_base
    poster_url = external_path(url_base, f"/api/files/{media.id}/poster/image")
    return FilePosterResponse(file_id=media.id, status="available", poster_url=poster_url)


@router.get("/files/{file_id}/poster/image")
def get_file_poster_image_endpoint(
    file_id: int, session: Session = Depends(get_session)
) -> Response:
    """Stream a tracked file's actual Plex poster image bytes, server-side (COL-212).

    What a ``poster_url`` from ``GET /api/files/{file_id}/poster`` points at.
    Re-resolves the same ``ratingKey`` (:func:`_resolve_poster_rating_key`)
    and, on a hit, fetches the image via
    :func:`collapsarr.plex.fetch_poster_image` and returns it directly with
    Plex's reported ``Content-Type`` -- the ``X-Plex-Token`` this proxies with
    is read server-side from the singleton
    :class:`~collapsarr.plex.models.PlexConnection` row and never appears in
    this response or any request the browser makes.

    Raises ``404`` for an unknown ``file_id``, an unresolvable ``ratingKey``,
    or a failed upstream fetch -- there is no placeholder-*image* fallback
    here: the frontend's ``<img onError>`` handler already swaps to the local
    placeholder graphic on any load failure (see the module docstring), so a
    plain ``404`` is exactly what it expects.
    """
    media = get_tracked_media_by_id(session, file_id)
    if media is None:
        raise HTTPException(status_code=404, detail=f"No tracked file with id={file_id}.")

    connection = get_plex_connection(session)
    rating_key = _resolve_poster_rating_key(session, media, connection)
    if rating_key is None:
        raise HTTPException(status_code=404, detail="No poster available for this file.")

    result = fetch_poster_image(connection.base_url, connection.token, rating_key)
    if not result.ok or result.content is None:
        raise HTTPException(status_code=404, detail="Poster image could not be retrieved.")

    return Response(content=result.content, media_type=result.content_type or "image/jpeg")
