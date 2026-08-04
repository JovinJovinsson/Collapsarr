"""Full-catalog fetch from a Sonarr/Radarr instance for the Library mirror (COL-98/COL-99).

Where :mod:`collapsarr.arr.files` pulls only the *monitored* file list that the
downmix-discovery path (COL-12) acts on, this module pulls a Sonarr or Radarr
instance's **entire** catalog *regardless* of the instance's own
``monitored``/``hasFile`` flags. That is the raw material the Library
(``collapsarr.library``) persists and keeps in sync: a mirror that includes
not-yet-downloaded episodes/movies (``hasFile: false``) so a user can set
their **Tracked** preference ahead of the file arriving.

Sonarr exposes its catalog across two endpoints:

- ``GET /api/v3/series`` -- every series, each with ``id``/``title`` and an
  embedded ``seasons`` array (each with a ``seasonNumber``).
- ``GET /api/v3/episode?seriesId=<id>`` -- every episode of a series, each with
  ``id``/``seasonNumber``/``episodeNumber``/``title``/``hasFile``. Unlike the
  ``episodefile`` endpoint :mod:`collapsarr.arr.files` uses, this lists episodes
  that have no file yet.

Radarr (COL-99) reports its whole catalog in a single call, same as
:mod:`collapsarr.arr.files`' Radarr path:

- ``GET /api/v3/movie`` -- every movie, each with ``id``/``title``/``hasFile``.
  No follow-up request is needed -- Radarr has no separate per-movie "file"
  endpoint to call.

Everything is normalized into frozen DTOs -- :class:`SonarrCatalog` /
:class:`CatalogSeries` / :class:`CatalogEpisode` for Sonarr,
:class:`RadarrCatalog` / :class:`CatalogMovie` for Radarr -- plain data, no
persistence (that is :mod:`collapsarr.library.service`'s job), mirroring how
:mod:`collapsarr.arr.files` returns plain :class:`~collapsarr.arr.files.MonitoredFile`
DTOs. Season identity is ``(series_id, season_number)`` -- Sonarr has no
standalone season object id -- while series, episodes, and movies carry the
Arr instance's own object ids.

Like :mod:`collapsarr.arr.files` (and unlike
:func:`collapsarr.arr.client.check_connectivity`), this lets ``httpx.HTTPError``
propagate: a failed fetch has no sensible empty default, so the caller (the
periodic scan) decides how to handle it.

Tests inject a ``transport`` (``httpx.MockTransport``) built from recorded
fixture responses instead of making real network calls.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .models import ArrInstance, InstanceType

_SERIES_PATH = "/api/v3/series"
_EPISODE_PATH = "/api/v3/episode"
_MOVIE_PATH = "/api/v3/movie"
_DEFAULT_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class CatalogEpisode:
    """One episode of a Sonarr series, present whether or not it has a file."""

    episode_id: int
    season_number: int
    episode_number: int
    title: str
    has_file: bool


@dataclass(frozen=True, slots=True)
class CatalogSeries:
    """One Sonarr series with its season numbers and every episode."""

    series_id: int
    title: str
    season_numbers: tuple[int, ...]
    episodes: tuple[CatalogEpisode, ...]


@dataclass(frozen=True, slots=True)
class SonarrCatalog:
    """A Sonarr instance's full Series > Season > Episode catalog."""

    instance_id: int
    series: tuple[CatalogSeries, ...]


@dataclass(frozen=True, slots=True)
class CatalogMovie:
    """One movie in a Radarr instance's catalog, present whether or not it has a file."""

    movie_id: int
    title: str
    has_file: bool


@dataclass(frozen=True, slots=True)
class RadarrCatalog:
    """A Radarr instance's full, flat Movie catalog."""

    instance_id: int
    movies: tuple[CatalogMovie, ...]


def fetch_sonarr_catalog(
    instance: ArrInstance,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> SonarrCatalog:
    """Fetch a Sonarr instance's entire catalog, normalized into DTOs.

    Every series and every episode is returned regardless of Sonarr's own
    ``monitored``/``hasFile`` flags -- the Library mirrors the whole catalog,
    including not-yet-downloaded episodes.

    Raises:
        ValueError: if ``instance`` is not a Sonarr instance (Radarr library
            support is a later ticket).
        httpx.HTTPError: on a network failure or a non-2xx response. Not
            swallowed -- an empty catalog would be indistinguishable from a
            wiped-out instance, and the scan must not soft-hide the whole
            library on a transient fetch failure.
    """
    if instance.type is not InstanceType.SONARR:
        raise ValueError(
            f"fetch_sonarr_catalog only supports Sonarr instances, got {instance.type!r}"
        )

    base_url = instance.base_url.rstrip("/")
    headers = {"X-Api-Key": instance.api_key}
    series_out: list[CatalogSeries] = []

    if transport is not None:
        client = httpx.Client(timeout=timeout, transport=transport)
    else:
        client = httpx.Client(timeout=timeout)

    with client:
        series_response = client.get(f"{base_url}{_SERIES_PATH}", headers=headers)
        series_response.raise_for_status()
        series_list = series_response.json()
        if not isinstance(series_list, list):
            return SonarrCatalog(instance_id=instance.id, series=())

        for series in series_list:
            if not isinstance(series, dict):
                continue
            series_id = series.get("id")
            title = series.get("title")
            if not isinstance(series_id, int) or not isinstance(title, str):
                continue

            episodes_response = client.get(
                f"{base_url}{_EPISODE_PATH}",
                params={"seriesId": series_id},
                headers=headers,
            )
            episodes_response.raise_for_status()
            episodes = _parse_episodes(episodes_response.json())

            season_numbers = _collect_season_numbers(series.get("seasons"), episodes)
            series_out.append(
                CatalogSeries(
                    series_id=series_id,
                    title=title,
                    season_numbers=season_numbers,
                    episodes=episodes,
                )
            )

    return SonarrCatalog(instance_id=instance.id, series=tuple(series_out))


def _parse_episodes(payload: object) -> tuple[CatalogEpisode, ...]:
    """Normalize a Sonarr ``/episode`` response into :class:`CatalogEpisode` DTOs."""
    if not isinstance(payload, list):
        return ()

    episodes: list[CatalogEpisode] = []
    for episode in payload:
        if not isinstance(episode, dict):
            continue
        episode_id = episode.get("id")
        season_number = episode.get("seasonNumber")
        if not isinstance(episode_id, int) or not isinstance(season_number, int):
            continue
        episode_number = episode.get("episodeNumber")
        title = episode.get("title")
        episodes.append(
            CatalogEpisode(
                episode_id=episode_id,
                season_number=season_number,
                episode_number=episode_number if isinstance(episode_number, int) else 0,
                title=title if isinstance(title, str) else "",
                has_file=bool(episode.get("hasFile")),
            )
        )
    return tuple(episodes)


def _collect_season_numbers(
    seasons_payload: object, episodes: tuple[CatalogEpisode, ...]
) -> tuple[int, ...]:
    """Merge season numbers from the series' ``seasons`` array and its episodes.

    Sonarr reports seasons both on the series object (the authoritative list,
    including seasons with no episodes yet) and implicitly via each episode's
    ``seasonNumber``. Taking the union is robust to either source being sparse.
    """
    numbers: set[int] = set()
    if isinstance(seasons_payload, list):
        for season in seasons_payload:
            if isinstance(season, dict) and isinstance(season.get("seasonNumber"), int):
                numbers.add(season["seasonNumber"])
    for episode in episodes:
        numbers.add(episode.season_number)
    return tuple(sorted(numbers))


def fetch_radarr_catalog(
    instance: ArrInstance,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> RadarrCatalog:
    """Fetch a Radarr instance's entire movie catalog, normalized into DTOs.

    Every movie is returned regardless of Radarr's own ``monitored``/``hasFile``
    flags -- the Library mirrors the whole catalog, including not-yet-downloaded
    movies. Unlike Sonarr, Radarr's ``GET /api/v3/movie`` reports the full
    catalog in a single call -- there is no follow-up per-movie request.

    Raises:
        ValueError: if ``instance`` is not a Radarr instance.
        httpx.HTTPError: on a network failure or a non-2xx response. Not
            swallowed -- see :func:`fetch_sonarr_catalog` for the rationale.
    """
    if instance.type is not InstanceType.RADARR:
        raise ValueError(
            f"fetch_radarr_catalog only supports Radarr instances, got {instance.type!r}"
        )

    base_url = instance.base_url.rstrip("/")
    headers = {"X-Api-Key": instance.api_key}

    if transport is not None:
        client = httpx.Client(timeout=timeout, transport=transport)
    else:
        client = httpx.Client(timeout=timeout)

    with client:
        response = client.get(f"{base_url}{_MOVIE_PATH}", headers=headers)
        response.raise_for_status()
        payload = response.json()

    return RadarrCatalog(instance_id=instance.id, movies=_parse_movies(payload))


def _parse_movies(payload: object) -> tuple[CatalogMovie, ...]:
    """Normalize a Radarr ``/movie`` response into :class:`CatalogMovie` DTOs."""
    if not isinstance(payload, list):
        return ()

    movies: list[CatalogMovie] = []
    for movie in payload:
        if not isinstance(movie, dict):
            continue
        movie_id = movie.get("id")
        title = movie.get("title")
        if not isinstance(movie_id, int) or not isinstance(title, str):
            continue
        movies.append(
            CatalogMovie(
                movie_id=movie_id,
                title=title,
                has_file=bool(movie.get("hasFile")),
            )
        )
    return tuple(movies)
