"""HTTP client for talking to a Plex Media Server (COL-209).

Mirrors :mod:`collapsarr.arr.client`'s shape: every call takes an injectable
``transport`` (an ``httpx.MockTransport`` in tests) and never raises --
network errors, timeouts, non-2xx responses, and malformed payloads are all
captured as a failed result so callers can persist/report the outcome
unconditionally, without a try/except of their own.

Three calls, matching COL-209's acceptance criteria:

* :func:`check_connectivity` -- the same connectivity + version check
  :mod:`collapsarr.plex.service` stamps onto :class:`~collapsarr.plex.models.
  PlexConnection` on save, the same way :mod:`collapsarr.arr.service` does for
  :class:`~collapsarr.arr.models.ArrInstance`.
* :func:`analyze_item` -- triggers Plex's own per-item "Analyze" (audio
  stream / chapter thumbnail) scan for a given ``ratingKey``. Consumed by a
  later ticket (COL-210 and beyond); this ticket only builds and tests the
  call itself.
* :func:`list_library_sections` -- lists the server's configured library
  sections (Movies/TV/etc.), each with its ``key``/``title``/``type``. Also
  consumed by later Plex-sync tickets.

COL-210 adds two more, both feeding the Plex Sync mapping table
(:mod:`collapsarr.plex.library_sync`):

* :func:`list_section_items` -- walks one library section's items, each
  carrying its ``ratingKey`` and every on-disk file path Plex reports for it
  (via ``Media`` -> ``Part`` -> ``file``). The full-sync pass calls this once
  per section to rebuild the whole path -> ``ratingKey`` map.
* :func:`search_items` -- a single ``/search`` query (scoped by a title), used
  as the *live fallback* when a file's path isn't in the mapping table yet:
  the caller filters the returned candidates by the file's known
  season/episode and takes the match's ``ratingKey`` (see
  :func:`collapsarr.plex.library_sync.resolve_rating_key`).

COL-212 adds one more:

* :func:`fetch_poster_image` -- fetches a single item's poster (``thumb``)
  image bytes, given its already-resolved ``ratingKey``. Consumed by
  :mod:`collapsarr.media.routes`'s poster-image endpoint, which streams the
  bytes back to the browser server-side so the ``X-Plex-Token`` never reaches
  the client.

Every call authenticates with the ``X-Plex-Token`` header -- the token is
supplied by the caller (:mod:`collapsarr.plex.service`, reading it from the
server-side-only :class:`~collapsarr.plex.models.PlexConnection` row) and
never logged or echoed back. Read calls also send ``Accept: application/json``
so Plex Media Server returns JSON instead of its default XML.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

_IDENTITY_PATH = "/"
_SECTIONS_PATH = "/library/sections"
_SECTION_ITEMS_PATH_TEMPLATE = "/library/sections/{section_key}/all"
_SEARCH_PATH = "/search"
_ANALYZE_PATH_TEMPLATE = "/library/metadata/{rating_key}/analyze"
_POSTER_PATH_TEMPLATE = "/library/metadata/{rating_key}/thumb"
_DEFAULT_TIMEOUT = 10.0
_ERROR_BODY_LIMIT = 500
_DEFAULT_POSTER_CONTENT_TYPE = "image/jpeg"

#: Plex's numeric ``type`` for an episode item -- passed as the ``type`` query
#: param to :func:`list_section_items` for a *show* section so ``/all`` returns
#: the file-bearing episodes rather than the show/season containers (which have
#: no ``Media``/``Part`` of their own). Movie sections need no such param: their
#: ``/all`` already returns the file-bearing movie items directly.
PLEX_EPISODE_TYPE = "4"

_JSON_HEADERS = {"Accept": "application/json"}


def _make_client(timeout: float, transport: httpx.BaseTransport | None) -> httpx.Client:
    if transport is not None:
        return httpx.Client(timeout=timeout, transport=transport)
    return httpx.Client(timeout=timeout)


def _auth_headers(token: str, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"X-Plex-Token": token}
    if extra:
        headers.update(extra)
    return headers


@dataclass(frozen=True, slots=True)
class ConnectivityResult:
    """Outcome of a connectivity/version check against a Plex server."""

    ok: bool
    version: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AnalyzeResult:
    """Outcome of triggering Plex's per-item Analyze scan."""

    ok: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PosterImageResult:
    """Outcome of fetching a Plex item's poster (``thumb``) image bytes (COL-212).

    ``content``/``content_type`` are only set on success. ``content_type``
    falls back to :data:`_DEFAULT_POSTER_CONTENT_TYPE` when Plex's response
    omits a ``Content-Type`` header, so a caller can always set a
    ``media_type`` on the proxied response.
    """

    ok: bool
    content: bytes | None = None
    content_type: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LibrarySection:
    """One entry from Plex's ``/library/sections`` listing."""

    key: str
    title: str
    type: str


@dataclass(frozen=True, slots=True)
class SectionsResult:
    """Outcome of listing a Plex server's configured library sections."""

    ok: bool
    sections: tuple[LibrarySection, ...] = field(default_factory=tuple)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PlexMediaItem:
    """One Plex library item, as returned by :func:`list_section_items`/:func:`search_items`.

    ``rating_key`` is Plex's own opaque per-item id (kept as the string Plex
    sends it as). ``file_paths`` is every on-disk path Plex reports for the item
    (a single item can have several parts / split files). The scoping fields --
    ``title``, ``grandparent_title`` (the show, for an episode), ``season_number``
    (Plex's ``parentIndex``), ``episode_number`` (Plex's ``index``), and ``type``
    (``"movie"``/``"episode"``/...) -- let the live-fallback caller
    (:func:`collapsarr.plex.library_sync.resolve_rating_key`) pick the right
    candidate out of a title search. ``section_key`` is the library section the
    item lives in, when known (always set from the walked section by
    :func:`list_section_items`; derived from ``librarySectionID`` when present in
    a :func:`search_items` result, else ``None``).
    """

    rating_key: str
    file_paths: tuple[str, ...] = field(default_factory=tuple)
    section_key: str | None = None
    title: str | None = None
    grandparent_title: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    type: str | None = None


@dataclass(frozen=True, slots=True)
class SectionItemsResult:
    """Outcome of listing/searching Plex library items.

    Shared by :func:`list_section_items` and :func:`search_items`.
    """

    ok: bool
    items: tuple[PlexMediaItem, ...] = field(default_factory=tuple)
    error: str | None = None


def check_connectivity(
    base_url: str,
    token: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> ConnectivityResult:
    """Call the server's root endpoint and report connectivity + version.

    Never raises: a blank/malformed base URL, network errors, timeouts,
    non-2xx responses, and malformed payloads are all captured as a failed
    :class:`ConnectivityResult` so the service layer can persist
    success/failure state without a try/except -- notably including the
    freshly-created, not-yet-configured :class:`~collapsarr.plex.models.
    PlexConnection` row, whose ``base_url`` defaults to ``""`` -- matching
    :func:`collapsarr.arr.client.check_connectivity`.
    """
    if not base_url:
        return ConnectivityResult(ok=False, error="No base URL configured")

    url = f"{base_url.rstrip('/')}{_IDENTITY_PATH}"
    client = _make_client(timeout, transport)

    try:
        with client:
            response = client.get(url, headers=_auth_headers(token, extra=_JSON_HEADERS))
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code}: {exc.response.text}"[:_ERROR_BODY_LIMIT]
        return ConnectivityResult(ok=False, error=detail)
    except (httpx.HTTPError, ValueError) as exc:
        # ``ValueError`` also covers httpx's own ``InvalidURL``/cookie-jar
        # parsing failures on a malformed (but non-empty) URL -- neither is
        # an ``httpx.HTTPError`` subclass, so without this the caller would
        # see an unhandled exception instead of a reported failure.
        return ConnectivityResult(ok=False, error=str(exc))

    try:
        payload = response.json()
    except ValueError:
        return ConnectivityResult(ok=False, error="Invalid JSON in identity response")

    container = payload.get("MediaContainer") if isinstance(payload, dict) else None
    version = container.get("version") if isinstance(container, dict) else None
    if not isinstance(version, str) or not version:
        return ConnectivityResult(ok=False, error="Identity response missing 'version' field")

    return ConnectivityResult(ok=True, version=version)


def analyze_item(
    base_url: str,
    token: str,
    rating_key: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> AnalyzeResult:
    """Trigger Plex's Analyze scan for a single item (its ``ratingKey``).

    Plex queues the analysis asynchronously and responds with an empty body,
    so success is purely "the request was accepted" (a 2xx status) -- there is
    no payload to validate, unlike :func:`check_connectivity`. Never raises --
    see :func:`check_connectivity`'s docstring for the full "never raises"
    contract, which this mirrors.
    """
    if not base_url:
        return AnalyzeResult(ok=False, error="No base URL configured")

    path = _ANALYZE_PATH_TEMPLATE.format(rating_key=rating_key)
    url = f"{base_url.rstrip('/')}{path}"
    client = _make_client(timeout, transport)

    try:
        with client:
            response = client.put(url, headers=_auth_headers(token))
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code}: {exc.response.text}"[:_ERROR_BODY_LIMIT]
        return AnalyzeResult(ok=False, error=detail)
    except (httpx.HTTPError, ValueError) as exc:
        return AnalyzeResult(ok=False, error=str(exc))

    return AnalyzeResult(ok=True)


def fetch_poster_image(
    base_url: str,
    token: str,
    rating_key: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> PosterImageResult:
    """Fetch a single item's poster (``thumb``) image bytes, given its ``ratingKey``.

    Server-side only: the caller (:mod:`collapsarr.media.routes`'s poster-image
    endpoint) streams ``content`` straight back to the browser with the
    resolved ``content_type``, so the ``X-Plex-Token`` this call authenticates
    with never reaches the client. Never raises -- see
    :func:`check_connectivity`'s docstring for the full "never raises"
    contract, which this mirrors. No ``Accept: application/json`` header is
    sent (unlike the metadata calls above), since the response body here is
    binary image data, not JSON.
    """
    if not base_url:
        return PosterImageResult(ok=False, error="No base URL configured")

    path = _POSTER_PATH_TEMPLATE.format(rating_key=rating_key)
    url = f"{base_url.rstrip('/')}{path}"
    client = _make_client(timeout, transport)

    try:
        with client:
            response = client.get(url, headers=_auth_headers(token))
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code}: {exc.response.text}"[:_ERROR_BODY_LIMIT]
        return PosterImageResult(ok=False, error=detail)
    except (httpx.HTTPError, ValueError) as exc:
        return PosterImageResult(ok=False, error=str(exc))

    content_type = response.headers.get("content-type") or _DEFAULT_POSTER_CONTENT_TYPE
    return PosterImageResult(ok=True, content=response.content, content_type=content_type)


def list_library_sections(
    base_url: str,
    token: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> SectionsResult:
    """List the server's configured library sections.

    A response whose ``MediaContainer`` carries no ``Directory`` list (a
    server with zero sections configured -- Plex omits the key entirely
    rather than sending an empty list) is a success with an empty tuple, not
    an error. Never raises -- see :func:`check_connectivity`'s docstring for
    the full "never raises" contract, which this mirrors.
    """
    if not base_url:
        return SectionsResult(ok=False, error="No base URL configured")

    url = f"{base_url.rstrip('/')}{_SECTIONS_PATH}"
    client = _make_client(timeout, transport)

    try:
        with client:
            response = client.get(url, headers=_auth_headers(token, extra=_JSON_HEADERS))
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code}: {exc.response.text}"[:_ERROR_BODY_LIMIT]
        return SectionsResult(ok=False, error=detail)
    except (httpx.HTTPError, ValueError) as exc:
        return SectionsResult(ok=False, error=str(exc))

    try:
        payload = response.json()
    except ValueError:
        return SectionsResult(ok=False, error="Invalid JSON in sections response")

    container = payload.get("MediaContainer") if isinstance(payload, dict) else None
    if not isinstance(container, dict):
        return SectionsResult(ok=False, error="Sections response missing 'MediaContainer'")

    raw_sections = container.get("Directory", [])
    if not isinstance(raw_sections, list):
        return SectionsResult(ok=False, error="Sections response 'Directory' is not a list")

    sections: list[LibrarySection] = []
    for entry in raw_sections:
        if not isinstance(entry, dict):
            continue
        key, title, section_type = entry.get("key"), entry.get("title"), entry.get("type")
        if not (isinstance(key, str) and isinstance(title, str) and isinstance(section_type, str)):
            continue
        sections.append(LibrarySection(key=key, title=title, type=section_type))

    return SectionsResult(ok=True, sections=tuple(sections))


def _coerce_index(value: object) -> int | None:
    """Coerce a Plex ``parentIndex``/``index`` (int, or digit string) to ``int``, else ``None``."""
    if isinstance(value, bool):  # bool is an int subclass -- never a valid index
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _extract_file_paths(entry: dict[str, object]) -> tuple[str, ...]:
    """Pull every ``Media[].Part[].file`` path off one Plex metadata entry."""
    paths: list[str] = []
    media_list = entry.get("Media")
    if not isinstance(media_list, list):
        return ()
    for media in media_list:
        if not isinstance(media, dict):
            continue
        parts = media.get("Part")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("file"), str):
                paths.append(part["file"])
    return tuple(paths)


def _parse_media_item(
    entry: object, *, default_section_key: str | None
) -> PlexMediaItem | None:
    """Parse one Plex metadata entry into a :class:`PlexMediaItem`, or ``None`` if unusable.

    An entry with no ``ratingKey`` can't be mapped to anything, so it is
    skipped. ``section_key`` prefers ``default_section_key`` (the walked
    section, for :func:`list_section_items`), falling back to the entry's own
    ``librarySectionID`` when present (a :func:`search_items` result spans
    sections, so each item carries its own).
    """
    if not isinstance(entry, dict):
        return None
    rating_key = entry.get("ratingKey")
    if isinstance(rating_key, int):
        rating_key = str(rating_key)
    if not isinstance(rating_key, str) or not rating_key:
        return None

    section_key = default_section_key
    if section_key is None:
        raw_section = entry.get("librarySectionID")
        if isinstance(raw_section, (int, str)) and not isinstance(raw_section, bool):
            section_key = str(raw_section)

    title = entry.get("title") if isinstance(entry.get("title"), str) else None
    grandparent = (
        entry.get("grandparentTitle")
        if isinstance(entry.get("grandparentTitle"), str)
        else None
    )
    item_type = entry.get("type") if isinstance(entry.get("type"), str) else None

    return PlexMediaItem(
        rating_key=rating_key,
        file_paths=_extract_file_paths(entry),
        section_key=section_key,
        title=title,
        grandparent_title=grandparent,
        season_number=_coerce_index(entry.get("parentIndex")),
        episode_number=_coerce_index(entry.get("index")),
        type=item_type,
    )


def _parse_items_response(
    response: httpx.Response, *, default_section_key: str | None
) -> SectionItemsResult:
    """Shared body parser for :func:`list_section_items`/:func:`search_items`.

    A ``MediaContainer`` with no ``Metadata`` list (a section/search with zero
    results -- Plex omits the key entirely) is a success with an empty tuple,
    not an error, mirroring :func:`list_library_sections`'s empty-``Directory``
    handling.
    """
    try:
        payload = response.json()
    except ValueError:
        return SectionItemsResult(ok=False, error="Invalid JSON in items response")

    container = payload.get("MediaContainer") if isinstance(payload, dict) else None
    if not isinstance(container, dict):
        return SectionItemsResult(ok=False, error="Items response missing 'MediaContainer'")

    raw_items = container.get("Metadata", [])
    if not isinstance(raw_items, list):
        return SectionItemsResult(ok=False, error="Items response 'Metadata' is not a list")

    items = [
        item
        for entry in raw_items
        if (item := _parse_media_item(entry, default_section_key=default_section_key)) is not None
    ]
    return SectionItemsResult(ok=True, items=tuple(items))


def list_section_items(
    base_url: str,
    token: str,
    section_key: str,
    *,
    item_type: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> SectionItemsResult:
    """List one library section's items, each with its ``ratingKey`` and file paths.

    ``item_type`` is Plex's numeric type filter appended as ``?type=`` -- pass
    :data:`PLEX_EPISODE_TYPE` for a *show* section so ``/all`` returns the
    file-bearing episodes rather than the show/season containers; leave it
    ``None`` for a movie section (whose ``/all`` already returns file-bearing
    items). A section with no items is a success with an empty tuple, not an
    error. Never raises -- see :func:`check_connectivity`'s docstring for the
    full "never raises" contract, which this mirrors.
    """
    if not base_url:
        return SectionItemsResult(ok=False, error="No base URL configured")

    path = _SECTION_ITEMS_PATH_TEMPLATE.format(section_key=section_key)
    url = f"{base_url.rstrip('/')}{path}"
    params = {"type": item_type} if item_type is not None else None
    client = _make_client(timeout, transport)

    try:
        with client:
            response = client.get(
                url, headers=_auth_headers(token, extra=_JSON_HEADERS), params=params
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code}: {exc.response.text}"[:_ERROR_BODY_LIMIT]
        return SectionItemsResult(ok=False, error=detail)
    except (httpx.HTTPError, ValueError) as exc:
        return SectionItemsResult(ok=False, error=str(exc))

    return _parse_items_response(response, default_section_key=section_key)


def search_items(
    base_url: str,
    token: str,
    query: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> SectionItemsResult:
    """Search the whole server for items matching ``query`` (a title), in one request.

    The single live query the ratingKey resolver falls back to on a mapping-table
    miss: it scopes by title, and the caller narrows the returned candidates by
    the file's known season/episode. Results span sections, so each item's
    ``section_key`` is derived from its own ``librarySectionID`` when present.
    An empty result set is a success with an empty tuple. Never raises -- see
    :func:`check_connectivity`'s docstring for the full "never raises" contract,
    which this mirrors.
    """
    if not base_url:
        return SectionItemsResult(ok=False, error="No base URL configured")

    url = f"{base_url.rstrip('/')}{_SEARCH_PATH}"
    client = _make_client(timeout, transport)

    try:
        with client:
            response = client.get(
                url, headers=_auth_headers(token, extra=_JSON_HEADERS), params={"query": query}
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code}: {exc.response.text}"[:_ERROR_BODY_LIMIT]
        return SectionItemsResult(ok=False, error=detail)
    except (httpx.HTTPError, ValueError) as exc:
        return SectionItemsResult(ok=False, error=str(exc))

    return _parse_items_response(response, default_section_key=None)
