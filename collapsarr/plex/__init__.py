"""Plex Integration: singleton connection config + HTTP client (COL-209).

Home for this epic's Plex-connectivity concerns per ``docs/TRACKER.md``'s
"Plex Integration" component: the singleton :class:`~collapsarr.plex.models.
PlexConnection` row (base URL + ``X-Plex-Token``), the service layer that
reads/writes it and re-validates connectivity on save, and an HTTP client
(connectivity check, per-item Analyze, library-section listing) that always
takes an injectable transport so no test needs real network access. Exposing
this over HTTP/UI is also this ticket's concern (:mod:`collapsarr.plex.routes`);
consuming the client's Analyze/section-listing calls for an actual Plex sync
is a later ticket's (COL-210 and beyond).

This module is imported for its side effect of registering
:class:`~collapsarr.plex.models.PlexConnection` with
:data:`collapsarr.database.Base.metadata` -- see
the Alembic migration environment (:mod:`collapsarr.migrations`).
"""

from __future__ import annotations

from .client import (
    PLEX_EPISODE_TYPE,
    AnalyzeResult,
    ConnectivityResult,
    LibrarySection,
    PlexMediaItem,
    SectionItemsResult,
    SectionsResult,
    analyze_item,
    check_connectivity,
    list_library_sections,
    list_section_items,
    search_items,
)
from .library_sync import rebuild_library_items, resolve_rating_key
from .models import (
    PLEX_CONNECTION_ID,
    ConnectivityStatus,
    PlexConnection,
    PlexLibraryItem,
)
from .service import get_plex_connection, update_plex_connection

__all__ = [
    "PLEX_CONNECTION_ID",
    "PLEX_EPISODE_TYPE",
    "AnalyzeResult",
    "ConnectivityResult",
    "ConnectivityStatus",
    "LibrarySection",
    "PlexConnection",
    "PlexLibraryItem",
    "PlexMediaItem",
    "SectionItemsResult",
    "SectionsResult",
    "analyze_item",
    "check_connectivity",
    "get_plex_connection",
    "list_library_sections",
    "list_section_items",
    "rebuild_library_items",
    "resolve_rating_key",
    "search_items",
    "update_plex_connection",
]
