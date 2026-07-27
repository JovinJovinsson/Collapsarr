"""Database restore module (Epic COL-62).

Phase 2 of the backup/restore story. COL-70 delivered the boot-time consumer:
:mod:`collapsarr.restore.engine` swaps a staged database into place on startup
when a marker (:mod:`collapsarr.restore.marker`) is present, taking a safety
backup of the current database first via the Phase 1 backup service.

COL-71 adds the producer: :mod:`collapsarr.restore.request` runs the
validate-before-stage gate against a listed backup and, on success, stages the
database and writes the marker; :mod:`collapsarr.restore.routes` exposes that
as ``POST /api/system/backup/restore/{id}`` and triggers this process's own
shutdown so the supervisor restarts it and the swap applies next boot.

A later slice adds the Alembic version-compatibility guard (COL-72), slotting
into the same gate :mod:`collapsarr.restore.request` establishes.
"""

from __future__ import annotations

from .engine import RestoreOutcome, apply_pending_restore
from .marker import (
    RESTORE_MARKER_FILENAME,
    RestoreMarker,
    clear_restore_marker,
    read_restore_marker,
    restore_marker_path,
    write_restore_marker,
)
from .request import RESTORE_STAGED_FILENAME, RestoreGateError, restore_staging_path, stage_restore
from .routes import router as restore_router

__all__ = [
    "RESTORE_MARKER_FILENAME",
    "RESTORE_STAGED_FILENAME",
    "RestoreGateError",
    "RestoreMarker",
    "RestoreOutcome",
    "apply_pending_restore",
    "clear_restore_marker",
    "read_restore_marker",
    "restore_marker_path",
    "restore_router",
    "restore_staging_path",
    "stage_restore",
    "write_restore_marker",
]
