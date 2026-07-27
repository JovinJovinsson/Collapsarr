"""Boot-time staged swap engine (COL-70).

The consumer half of the restore flow. On startup -- before the application's
database engine connects and before the Alembic schema upgrade
(:func:`collapsarr.migrations.upgrade_to_head`) -- :func:`apply_pending_restore`
looks for a restore marker (:mod:`collapsarr.restore.marker`) and, if one is
present and points at a valid staged SQLite database, swaps that file into place
as the live database. This is the single window where a raw file swap is
provably safe: nothing has opened the database yet, so there is no concurrent
writer and no open connection to invalidate.

Sequence when a valid restore is pending:

#. Take a **safety backup of the current database** through the Phase 1 backup
   service, so a mistaken restore is itself reversible. It is emitted as a
   ``restore``-type backup -- a pre-destructive, boot-path rollback point (the
   same shape as the pre-migration ``update`` backup) but in its own
   subdirectory, so the pre-migration backup a forward-migrating restore takes
   moments later can't collide with and overwrite it. It lands as a listable
   backup like any other.
#. **Swap** the staged file into place atomically.
#. **Clear the marker**, then let the normal ``upgrade_to_head`` run -- which
   forward-migrates an older restored database up to head before serving.

Defensive path: if the staged file is missing or is not a valid SQLite database
(or the configured database is not a file-based SQLite one at all), the swap is
**aborted** -- the current database is left untouched, the failure is logged
loudly, the marker is cleared, and the app boots normally. The marker is always
consumed exactly once (cleared on success and on every abort), so a restore can
never loop across reboots.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from collapsarr.backup.service import (
    BACKUP_RESTORE,
    BackupInfo,
    _raw_copy_snapshot,
    create_backup,
    resolve_sqlite_path,
)
from collapsarr.config import Settings
from collapsarr.restore.marker import (
    clear_restore_marker,
    read_restore_marker,
    restore_marker_path,
)

logger = logging.getLogger(__name__)

#: The 16-byte header every SQLite 3 database file starts with. A cheap, robust
#: "is this actually a SQLite database?" check for the staged file -- a truncated
#: file, a stray text file, or anything non-SQLite fails it.
_SQLITE_MAGIC = b"SQLite format 3\x00"

#: Journal/WAL sidecar suffixes of the *previous* database. They are deleted
#: after a swap so a stale WAL/SHM from the replaced database can never be
#: misapplied on top of the freshly restored file. The staged file is a single
#: self-contained database (a backup snapshot), so it carries no sidecars of its
#: own.
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    """Result of a boot-time restore attempt (for logging/tests).

    ``applied`` is ``True`` only when the staged file was swapped in. On a
    defensive abort it is ``False`` and ``reason`` explains why. ``safety_backup``
    is the :class:`~collapsarr.backup.service.BackupInfo` of the pre-swap safety
    backup, or ``None`` when the swap was aborted or there was no current
    database file to back up.
    """

    applied: bool
    reason: str | None = None
    safety_backup: BackupInfo | None = None


def _is_sqlite_file(path: Path) -> bool:
    """Return whether ``path`` exists and looks like a SQLite database file."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except OSError:
        return False


def apply_pending_restore(settings: Settings) -> RestoreOutcome | None:
    """Apply a pending staged-database restore, if one is marked.

    Returns ``None`` when there is no marker (a clean no-op boot). Otherwise
    returns a :class:`RestoreOutcome` describing whether the swap was applied or
    defensively aborted. Must be called on the boot path *before* the database
    engine connects and *before* ``upgrade_to_head``.
    """
    marker_path = restore_marker_path(settings)
    if not marker_path.is_file():
        # No restore pending: clean no-op, no filesystem writes.
        return None

    try:
        marker = read_restore_marker(settings)
        if marker is None:
            # read_restore_marker already logged the specifics of the corruption.
            logger.error(
                "RESTORE ABORTED: the restore marker at %s is unreadable; booting "
                "on the current database.",
                marker_path,
            )
            return RestoreOutcome(applied=False, reason="malformed restore marker")

        staged = marker.staged_path
        if not _is_sqlite_file(staged):
            logger.error(
                "RESTORE ABORTED: staged database %s is missing or is not a valid "
                "SQLite file. The current database is left untouched; booting normally.",
                staged,
            )
            return RestoreOutcome(
                applied=False, reason="staged file missing or not a SQLite database"
            )

        current_db = resolve_sqlite_path(settings)
        if current_db is None:
            logger.error(
                "RESTORE ABORTED: the configured database is not a file-based SQLite "
                "database, so a staged-file swap cannot be applied. The current "
                "database is left untouched; booting normally."
            )
            return RestoreOutcome(
                applied=False, reason="configured database is not file-based SQLite"
            )

        safety_backup = _safety_backup_current_db(settings, current_db)
        _swap_in(staged, current_db)
        logger.warning(
            "RESTORE APPLIED: swapped staged database %s into %s. The schema upgrade "
            "will now migrate it forward to head if it is an older revision.",
            staged,
            current_db,
        )
        return RestoreOutcome(applied=True, safety_backup=safety_backup)
    finally:
        # A marker is consumed exactly once -- cleared on success, on a defensive
        # abort, and even on an unexpected error -- so a restore never loops.
        clear_restore_marker(settings)


def _safety_backup_current_db(settings: Settings, current_db: Path) -> BackupInfo | None:
    """Snapshot the current database before it is overwritten by the swap.

    Emitted through the Phase 1 backup service as a unified ``update``-type
    backup (listable alongside manual/scheduled/pre-migration backups). Runs
    before the engine connects, so -- like the pre-migration backup -- a
    byte-for-byte raw copy is both safe and the exact pre-restore rollback
    artifact.

    No-op when the current database file doesn't exist yet: a fresh install has
    no prior data to protect, so there is nothing to back up.
    """
    if not current_db.exists():
        logger.info(
            "No safety backup taken before restore: no current database file at %s yet.",
            current_db,
        )
        return None
    info = create_backup(settings, BACKUP_RESTORE, snapshot=_raw_copy_snapshot)
    logger.info("Safety backup of the current database taken before restore: %s", info.id)
    return info


def _swap_in(staged: Path, current_db: Path) -> None:
    """Atomically replace ``current_db`` with the ``staged`` file.

    The staged bytes are copied to a temp file in ``current_db``'s own directory
    and then :func:`os.replace`-d onto it -- an atomic rename on the same
    filesystem, so the live database is only ever the old file or the new one,
    never a partial write. Stale sidecars of the replaced database are removed,
    and the (now consumed) staged file is deleted.
    """
    current_db.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = current_db.with_name(f".{current_db.name}.restore.part")
    try:
        shutil.copy2(staged, tmp_path)
        os.replace(tmp_path, current_db)
    finally:
        tmp_path.unlink(missing_ok=True)

    for suffix in _SIDECAR_SUFFIXES:
        current_db.with_name(current_db.name + suffix).unlink(missing_ok=True)
    staged.unlink(missing_ok=True)


__all__ = [
    "RestoreOutcome",
    "apply_pending_restore",
]
