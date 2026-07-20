"""Pull the monitored media-file list from a configured Sonarr/Radarr instance.

Per the PRD, Collapsarr discovers work by pulling *monitored* file lists from
the Arr APIs rather than scanning arbitrary folders itself. Sonarr and Radarr
expose that information differently:

- Sonarr has no single "all monitored episode files" endpoint. Series are
  fetched via ``GET /api/v3/series`` (each with a ``monitored`` flag), and for
  every *monitored* series its files are fetched via
  ``GET /api/v3/episodefile?seriesId=<id>``.
- Radarr's ``GET /api/v3/movie`` returns every movie in one call, each with
  ``monitored``/``hasFile`` flags and (when present) an embedded
  ``movieFile`` object — no second request needed.

Both variants are normalized to the same :class:`MonitoredFile` shape and
reached through the single :func:`fetch_monitored_files` entry point, which
dispatches on :attr:`~collapsarr.arr.models.ArrInstance.type` so callers don't
need to special-case Sonarr vs. Radarr.

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
    """A single monitored media file, normalized across Sonarr and Radarr."""

    instance_id: int
    media_title: str
    file_path: str
    source_file_id: int | None = None
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
            results.append(
                MonitoredFile(
                    instance_id=instance.id,
                    media_title=title,
                    file_path=path,
                    source_file_id=file_id if isinstance(file_id, int) else None,
                    audio=_extract_audio_info(movie_file.get("mediaInfo")),
                )
            )

    return results
