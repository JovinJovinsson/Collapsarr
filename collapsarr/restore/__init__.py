"""Database restore module (Epic COL-62).

Phase 2 of the backup/restore story. This slice (COL-70) delivers the boot-time
consumer: :mod:`collapsarr.restore.engine` swaps a staged database into place on
startup when a marker (:mod:`collapsarr.restore.marker`) is present, taking a
safety backup of the current database first via the Phase 1 backup service.

Later slices add the restore-request endpoints that write the marker (COL-71)
and the Alembic version-compatibility guard (COL-72), building on the marker
seam established here.
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

__all__ = [
    "RESTORE_MARKER_FILENAME",
    "RestoreMarker",
    "RestoreOutcome",
    "apply_pending_restore",
    "clear_restore_marker",
    "read_restore_marker",
    "restore_marker_path",
    "write_restore_marker",
]
