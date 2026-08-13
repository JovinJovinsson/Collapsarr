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
_ANALYZE_PATH_TEMPLATE = "/library/metadata/{rating_key}/analyze"
_DEFAULT_TIMEOUT = 10.0
_ERROR_BODY_LIMIT = 500

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
