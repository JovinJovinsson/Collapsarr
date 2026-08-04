"""Pull the monitored media-file list from a configured Sonarr/Radarr instance.

Per the PRD, Collapsarr discovers work by pulling *monitored* file lists from
the Arr APIs rather than scanning arbitrary folders itself. Sonarr and Radarr
expose that information differently:

- Sonarr has no single "all monitored episode files" endpoint. Series are
  fetched via ``GET /api/v3/series`` (each with a ``monitored`` flag), and for
  every *monitored* series its files are fetched via
  ``GET /api/v3/episodefile?seriesId=<id>``. Sonarr's ``episodefile`` objects
  carry no ``episodeId`` of their own (a file can cover more than one episode
  for a multi-episode release), so resolving *which* episode(s) a file
  belongs to means cross-referencing ``GET /api/v3/episode?seriesId=<id>``
  (the same endpoint :mod:`collapsarr.arr.catalog` already calls for the
  Library mirror) and matching on each episode's own ``episodeFileId``
  back-reference (COL-101).
- Radarr's ``GET /api/v3/movie`` returns every movie in one call, each with
  ``monitored``/``hasFile`` flags and (when present) an embedded
  ``movieFile`` object — no second request needed; the movie's own ``id`` is
  already in scope.

Both variants are normalized to the same :class:`MonitoredFile` shape and
reached through the single :func:`fetch_monitored_files` entry point, which
dispatches on :attr:`~collapsarr.arr.models.ArrInstance.type` so callers don't
need to special-case Sonarr vs. Radarr. Each carries the Arr instance's own
``sonarr_episode_id``/``radarr_movie_id`` (COL-101) when resolvable -- the
bridge :func:`~collapsarr.media.service.upsert_tracked_media` persists onto
:class:`~collapsarr.media.models.TrackedMediaFile` so a scanned file's
**Tracked** value (owned by the matching
:class:`~collapsarr.library.models.LibraryNode`) can be looked up without
parsing ``file_path`` (``CONTEXT.md`` rules that out -- paths aren't a stable
catalog identity). A multi-episode Sonarr file resolves to its *first*
matching episode id -- good enough to link back to *a* Tracked value, even
though technically more than one episode shares the file.

Audio metadata is taken from the Arr APIs' ``mediaInfo`` block, which reports
*aggregate* fields (codec, total channel count, language list, stream count)
rather than a per-stream breakdown — that is the granularity normalized into
:class:`AudioInfo`. A per-stream probe (e.g. via ffprobe) is out of scope here.

Unlike :func:`collapsarr.arr.client.check_connectivity`, which never raises so
a connectivity outcome can always be persisted, this module lets
``httpx.HTTPError`` propagate: a failed fetch has no sensible default (an
empty list would be indistinguishable from "no monitored files"), so callers
decide how to handle it.

Tests inject a ``transport`` (``httpx.MockTransport``) built from recorded
fixture responses instead of making real network calls.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .models import ArrInstance, InstanceType

_SERIES_PATH = "/api/v3/series"
_EPISODE_FILE_PATH = "/api/v3/episodefile"
_EPISODE_PATH = "/api/v3/episode"
_MOVIE_PATH = "/api/v3/movie"
_DEFAULT_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class AudioInfo:
    """Aggregate audio-stream metadata as exposed by an Arr API's ``mediaInfo``.

    All fields are ``None`` when the underlying file has no ``mediaInfo``
    block, or when a given sub-field wasn't present/wasn't of the expected
    type.
    """

    codec: str | None = None
    channels: float | None = None
    languages: str | None = None
    stream_count: int | None = None


@dataclass(frozen=True, slots=True)
class MonitoredFile:
    """A single monitored media file, normalized across Sonarr and Radarr.

    ``sonarr_episode_id``/``radarr_movie_id`` (COL-101) are the Arr
    instance's own object ids for the episode/movie this file belongs to --
    distinct from ``source_file_id`` (the *file* row's own id, e.g. Sonarr's
    ``episodefile.id``). Exactly one is set, matching ``instance_id``'s Arr
    instance type; both are ``None`` only if the id genuinely couldn't be
    resolved (e.g. a Sonarr file whose ``episodefile.id`` has no matching
    ``episode.episodeFileId``, which shouldn't happen for a well-formed Sonarr
    response but is handled rather than assumed).
    """

    instance_id: int
    media_title: str
    file_path: str
    source_file_id: int | None = None
    sonarr_episode_id: int | None = None
    radarr_movie_id: int | None = None
    audio: AudioInfo | None = None


def fetch_monitored_files(
    instance: ArrInstance,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> list[MonitoredFile]:
    """Fetch the normalized monitored-file list for a configured instance.

    Dispatches to the Sonarr or Radarr variant based on ``instance.type`` —
    both return the same :class:`MonitoredFile` shape.

    Raises:
        httpx.HTTPError: on a network failure or a non-2xx response from the
            instance. This function does not swallow errors the way
            :func:`collapsarr.arr.client.check_connectivity` does.
    """
    if instance.type is InstanceType.SONARR:
        return _fetch_sonarr_files(instance, timeout=timeout, transport=transport)
    if instance.type is InstanceType.RADARR:
        return _fetch_radarr_files(instance, timeout=timeout, transport=transport)
    raise ValueError(f"Unsupported instance type: {instance.type!r}")  # pragma: no cover


def _build_client(timeout: float, transport: httpx.BaseTransport | None) -> httpx.Client:
    if transport is not None:
        return httpx.Client(timeout=timeout, transport=transport)
    return httpx.Client(timeout=timeout)


def _extract_audio_info(media_info: object) -> AudioInfo | None:
    """Normalize an Arr ``mediaInfo`` block into :class:`AudioInfo`, or ``None``."""
    if not isinstance(media_info, dict):
        return None

    codec = media_info.get("audioCodec")
    channels = media_info.get("audioChannels")
    languages = media_info.get("audioLanguages")
    stream_count = media_info.get("audioStreamCount")

    return AudioInfo(
        codec=codec if isinstance(codec, str) else None,
        channels=float(channels) if isinstance(channels, int | float) else None,
        languages=languages if isinstance(languages, str) else None,
        stream_count=stream_count if isinstance(stream_count, int) else None,
    )


def _episode_id_by_file_id(episodes_payload: object) -> dict[int, int]:
    """Map ``episodefile.id`` -> ``episode.id`` from a Sonarr ``/episode`` response.

    Sonarr's episode objects carry the back-reference (``episodeFileId``); the
    ``episodefile`` objects :func:`_fetch_sonarr_files` iterates don't carry
    the forward one, so this is built once per series and used to resolve
    each file's owning episode id (COL-101). When more than one episode
    shares a file (a multi-episode release), the *first* one encountered
    wins -- good enough to link back to a Tracked value, even though more
    than one episode technically shares the file.
    """
    mapping: dict[int, int] = {}
    if not isinstance(episodes_payload, list):
        return mapping
    for episode in episodes_payload:
        if not isinstance(episode, dict):
            continue
        episode_id = episode.get("id")
        episode_file_id = episode.get("episodeFileId")
        if (
            isinstance(episode_id, int)
            and isinstance(episode_file_id, int)
            and episode_file_id
            and episode_file_id not in mapping
        ):
            mapping[episode_file_id] = episode_id
    return mapping


def _fetch_sonarr_files(
    instance: ArrInstance, *, timeout: float, transport: httpx.BaseTransport | None
) -> list[MonitoredFile]:
    base_url = instance.base_url.rstrip("/")
    headers = {"X-Api-Key": instance.api_key}
    results: list[MonitoredFile] = []

    with _build_client(timeout, transport) as client:
        series_response = client.get(f"{base_url}{_SERIES_PATH}", headers=headers)
        series_response.raise_for_status()
        series_list = series_response.json()
        if not isinstance(series_list, list):
            return results

        for series in series_list:
            if not isinstance(series, dict) or not series.get("monitored"):
                continue
            series_id = series.get("id")
            series_title = series.get("title")
            if series_id is None or not isinstance(series_title, str):
                continue

            files_response = client.get(
                f"{base_url}{_EPISODE_FILE_PATH}",
                params={"seriesId": series_id},
                headers=headers,
            )
            files_response.raise_for_status()
            episode_files = files_response.json()
            if not isinstance(episode_files, list):
                continue

            episodes_response = client.get(
                f"{base_url}{_EPISODE_PATH}",
                params={"seriesId": series_id},
                headers=headers,
            )
            episodes_response.raise_for_status()
            episode_id_by_file_id = _episode_id_by_file_id(episodes_response.json())

            for episode_file in episode_files:
                if not isinstance(episode_file, dict):
                    continue
                path = episode_file.get("path")
                if not isinstance(path, str) or not path:
                    continue
                file_id = episode_file.get("id")
                results.append(
                    MonitoredFile(
                        instance_id=instance.id,
                        media_title=series_title,
                        file_path=path,
                        source_file_id=file_id if isinstance(file_id, int) else None,
                        sonarr_episode_id=(
                            episode_id_by_file_id.get(file_id)
                            if isinstance(file_id, int)
                            else None
                        ),
                        audio=_extract_audio_info(episode_file.get("mediaInfo")),
                    )
                )

    return results


def _fetch_radarr_files(
    instance: ArrInstance, *, timeout: float, transport: httpx.BaseTransport | None
) -> list[MonitoredFile]:
    base_url = instance.base_url.rstrip("/")
    headers = {"X-Api-Key": instance.api_key}
    results: list[MonitoredFile] = []

    with _build_client(timeout, transport) as client:
        response = client.get(f"{base_url}{_MOVIE_PATH}", headers=headers)
        response.raise_for_status()
        movies = response.json()
        if not isinstance(movies, list):
            return results

        for movie in movies:
            if not isinstance(movie, dict):
                continue
            if not movie.get("monitored") or not movie.get("hasFile"):
                continue
            movie_file = movie.get("movieFile")
            if not isinstance(movie_file, dict):
                continue
            path = movie_file.get("path")
            title = movie.get("title")
            if not isinstance(path, str) or not path or not isinstance(title, str):
                continue
            file_id = movie_file.get("id")
            movie_id = movie.get("id")
            results.append(
                MonitoredFile(
                    instance_id=instance.id,
                    media_title=title,
                    file_path=path,
                    source_file_id=file_id if isinstance(file_id, int) else None,
                    radarr_movie_id=movie_id if isinstance(movie_id, int) else None,
                    audio=_extract_audio_info(movie_file.get("mediaInfo")),
                )
            )

    return results
