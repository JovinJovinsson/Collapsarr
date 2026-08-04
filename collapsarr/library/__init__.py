"""Library: a per-instance mirror of a Sonarr catalog with Tracked state (COL-98).

Home for Library concerns (``CONTEXT.md``'s "Library" / "Library Node"): a
persisted Series > Season > Episode tree per configured
:class:`~collapsarr.arr.models.ArrInstance`, kept in sync by the periodic scan
(:mod:`collapsarr.jobs.scheduler`), with each node's nullable **Tracked**
override, ancestor-override resolution, cascade-on-toggle, and soft-hide-on-
disappearance semantics.

This module is imported for its side effect of registering
:class:`~collapsarr.library.models.LibraryNode` with
:data:`collapsarr.database.Base.metadata` -- see the Alembic migration
environment (:mod:`collapsarr.migrations`).
"""

from __future__ import annotations

from .models import LibraryNode, LibraryNodeKind, make_node_key
from .service import (
    LibraryNodeNotFoundError,
    LibraryTree,
    TreeEpisode,
    TreeSeason,
    TreeSeries,
    build_tree,
    get_node,
    list_nodes,
    resolve_tracked,
    set_tracked,
    sync_library,
)

__all__ = [
    "LibraryNode",
    "LibraryNodeKind",
    "LibraryNodeNotFoundError",
    "LibraryTree",
    "TreeEpisode",
    "TreeSeason",
    "TreeSeries",
    "build_tree",
    "get_node",
    "list_nodes",
    "make_node_key",
    "resolve_tracked",
    "set_tracked",
    "sync_library",
]
