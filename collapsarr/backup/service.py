"""Backup service: safe live SQLite snapshots into zipped archives (COL-63).

The core of the backup system's walking skeleton. A backup is a single
``VACUUM INTO`` snapshot of the live database -- a guaranteed-consistent copy
taken against the running file without stopping the app -- zipped into a
type-partitioned directory under ``<data_dir>/backups/``:

``<data_dir>/backups/{manual,scheduled,update}/collapsarr_backup_v<ver>_<ts>.zip``

Only the ``manual`` type is produced by this slice (via the "Backup Now"
endpoint); ``scheduled`` and ``update`` are part of the on-disk layout now so
the later Epic slices (scheduler, pre-migration fold) drop straight into it.

Safety contract (mirrors the pre-migration backup in
:mod:`collapsarr.migrations`):

* The whole write runs under a single process-wide lock
  (:data:`_BACKUP_LOCK`), so concurrent "Backup Now" clicks can't interleave
  their temp files or races the atomic rename.
* The snapshot is vacuumed to a temp file and the zip is built at a
  dot-prefixed temp path, then :func:`os.replace`-d onto the final name -- an
  atomic rename on the same filesystem. A failure anywhere before that rename
  leaves no file matching :data:`BACKUP_FILENAME_GLOB`, and the ``finally``
  cleanup removes both temp files, so a partial/failed backup never appears in
  :func:`list_backups`.
* Non-file-based databases no-op cleanly: :func:`resolve_sqlite_path` returns
  ``None`` for a non-SQLite ``database_url`` override or an in-memory
  (``:memory:``) database, and :func:`create_backup` raises
  :class:`BackupUnavailableError` rather than write a meaningless snapshot.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import sqlite3
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

from collapsarr import __version__
from collapsarr.config import Settings

# Reuse the migration module's SQLite-file resolver verbatim (COL-60) so the
# backup service and the pre-migration backup agree exactly on what counts as a
# file-based SQLite database -- a non-SQLite ``database_url`` override and the
# ``:memory:`` sentinel both resolve to ``None`` and no-op the snapshot.
from collapsarr.migrations import _sqlite_file_path as resolve_sqlite_path

logger = logging.getLogger(__name__)

#: Backup type -> subdirectory name under ``<data_dir>/backups/``. ``manual`` is
#: the only type produced by this slice; the other two are reserved for the
#: scheduler (COL-67) and the pre-migration fold (COL-69).
BACKUP_MANUAL = "manual"
BACKUP_SCHEDULED = "scheduled"
BACKUP_UPDATE = "update"

#: All valid backup types, in a stable order. The on-disk layout carves one
#: subdirectory per entry (see :func:`ensure_backup_dirs`).
BACKUP_TYPES: tuple[str, ...] = (BACKUP_MANUAL, BACKUP_SCHEDULED, BACKUP_UPDATE)

#: Glob matching a *finished* backup archive. Deliberately excludes the
#: dot-prefixed temp files an in-flight backup writes, so a partial/failed
#: backup is never listed.
BACKUP_FILENAME_GLOB = "collapsarr_backup_v*.zip"

#: Name the snapshot is stored under *inside* the zip. Fixed (not the source
#: filename) so a restore step can find the database member deterministically.
ARCHIVE_MEMBER_NAME = "collapsarr.db"

#: Minimum number of backups that must always remain on disk. A manual delete
#: (COL-65) refuses when it would drop the total below this floor, so an
#: operator can never delete their way to zero recovery points. The age-based
#: retention prune (COL-68) reuses this as a hard floor -- backups are never
#: pruned below this count regardless of age.
MINIMUM_BACKUP_KEEP = 1

#: Guaranteed-minimum count of ``update`` (pre-migration) backups the age-based
#: prune keeps *regardless of age* (COL-68). An ``update`` backup is the
#: rollback point for a schema migration, so an upgrade must remain reversible
#: even long after the retention window has lapsed. This floor is applied only
#: to the ``update`` type, on top of the global :data:`MINIMUM_BACKUP_KEEP`
#: floor every type honours -- keeping the two most recent lets an operator roll
#: back the last upgrade even when a newer upgrade has since run.
UPDATE_BACKUP_MIN_KEEP = 2

#: Serialises the whole create-backup critical section so concurrent requests
#: never interleave temp files or race the atomic rename.
_BACKUP_LOCK = Lock()


class BackupUnavailableError(RuntimeError):
    """A backup was requested but the database isn't file-based SQLite.

    Raised by :func:`create_backup` when :func:`resolve_sqlite_path` returns
    ``None`` (a non-SQLite ``database_url`` override or an in-memory database).
    The REST layer maps this to a ``409`` and the UI shows its "unavailable for
    this database configuration" state instead of the controls.
    """


class BackupNotFoundError(RuntimeError):
    """The named backup id doesn't resolve to a real archive on disk.

    Raised by :func:`delete_backup` when
    :func:`resolve_backup_path` returns ``None`` (unknown type, malformed or
    traversal id, or no file present). The REST layer maps this to a ``404``.
    """


class BackupRetentionFloorError(RuntimeError):
    """Deleting the backup would drop below :data:`MINIMUM_BACKUP_KEEP`.

    Raised by :func:`delete_backup` instead of executing the deletion, so an
    operator can't manually delete their last recovery point. The REST layer
    maps this to a ``409`` and surfaces the message to the UI.
    """


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """Summary of one backup archive on disk.

    ``id`` is the ``<type>/<filename>`` path relative to the backups root --
    unambiguous across the type subdirectories and safe for the later
    download/delete slices to resolve back to a path. ``created_at`` is derived
    from the file's modification time (UTC).
    """

    id: str
    name: str
    type: str
    size: int
    created_at: datetime


def _validate_type(backup_type: str) -> None:
    if backup_type not in BACKUP_TYPES:
        raise ValueError(
            f"Unknown backup type {backup_type!r}; expected one of {BACKUP_TYPES}."
        )


def backups_root(settings: Settings) -> Path:
    """Return the ``<data_dir>/backups`` root directory (not created here)."""
    return Path(settings.data_dir).expanduser() / "backups"


def backup_type_dir(settings: Settings, backup_type: str) -> Path:
    """Return the per-type subdirectory (e.g. ``.../backups/manual``)."""
    _validate_type(backup_type)
    return backups_root(settings) / backup_type


def ensure_backup_dirs(settings: Settings) -> None:
    """Create the backups root and every per-type subdirectory if missing."""
    for backup_type in BACKUP_TYPES:
        backup_type_dir(settings, backup_type).mkdir(parents=True, exist_ok=True)


def is_backup_supported(settings: Settings) -> bool:
    """Whether the configured database is a file-based SQLite one we can snapshot."""
    return resolve_sqlite_path(settings) is not None


def _backup_filename(now: datetime) -> str:
    """Build a backup filename: ``collapsarr_backup_v<ver>_<yyyy.MM.dd_HH.mm.ss>.zip``.

    ``now`` must be timezone-aware UTC; the timestamp is rendered from it
    verbatim (the caller passes ``datetime.now(UTC)``).
    """
    return f"collapsarr_backup_v{__version__}_{now:%Y.%m.%d_%H.%M.%S}.zip"


def _info_from_path(path: Path, backup_type: str) -> BackupInfo:
    """Adapt an on-disk archive path into a :class:`BackupInfo`."""
    stat = path.stat()
    return BackupInfo(
        id=f"{backup_type}/{path.name}",
        name=path.name,
        type=backup_type,
        size=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


def list_backups(settings: Settings) -> list[BackupInfo]:
    """List every finished backup across all type subdirectories, newest first.

    Missing directories are skipped (a fresh install has written none yet), and
    the dot-prefixed temp files of an in-flight backup are excluded by
    :data:`BACKUP_FILENAME_GLOB`.
    """
    root = backups_root(settings)
    infos: list[BackupInfo] = []
    for backup_type in BACKUP_TYPES:
        type_dir = root / backup_type
        if not type_dir.is_dir():
            continue
        for archive in type_dir.glob(BACKUP_FILENAME_GLOB):
            if archive.is_file():
                infos.append(_info_from_path(archive, backup_type))
    infos.sort(key=lambda info: info.created_at, reverse=True)
    return infos


def resolve_backup_path(settings: Settings, backup_id: str) -> Path | None:
    """Resolve a :attr:`BackupInfo.id` (``<type>/<filename>``) back to its archive path.

    ``None`` (never an exception) covers every way an ``id`` can fail to name a
    real archive: it doesn't split into exactly ``type/filename``, the type
    isn't one of :data:`BACKUP_TYPES`, the filename isn't a bare backup-archive
    name (rejecting empty/``.``/``..``/embedded separators rules out path
    traversal such as ``manual/../../etc/passwd``), it doesn't match
    :data:`BACKUP_FILENAME_GLOB`, or no file exists at the resolved path. The
    download route maps every ``None`` uniformly to a ``404`` so a probing
    client can't distinguish "wrong type" from "file doesn't exist".
    """
    parts = backup_id.split("/")
    if len(parts) != 2:
        return None
    backup_type, filename = parts
    if backup_type not in BACKUP_TYPES:
        return None
    if filename in ("", ".", "..") or "/" in filename or "\\" in filename:
        return None
    if not fnmatch.fnmatch(filename, BACKUP_FILENAME_GLOB):
        return None
    candidate = backup_type_dir(settings, backup_type) / filename
    if candidate.name != filename or not candidate.is_file():
        return None
    return candidate


def _snapshot_database(db_file: Path, destination: Path) -> None:
    """Write a consistent snapshot of ``db_file`` to ``destination`` via ``VACUUM INTO``.

    ``VACUUM INTO`` produces a single, self-contained, guaranteed-consistent
    copy of the live database without a separate journal/WAL, which is exactly
    what we want to archive. The destination must not already exist, so any
    stale temp file is removed first.
    """
    destination.unlink(missing_ok=True)
    connection = sqlite3.connect(str(db_file))
    try:
        connection.execute("VACUUM INTO ?", (str(destination),))
    finally:
        connection.close()


def _raw_copy_snapshot(db_file: Path, destination: Path) -> None:
    """Byte-for-byte copy of ``db_file`` to ``destination`` (pre-migration use).

    The snapshot strategy the pre-migration ``update`` backup passes to
    :func:`create_backup` (COL-69). It runs on the boot path *before the app's
    engine connects*, so there is no concurrent writer and a plain file copy is
    already consistent -- no ``VACUUM INTO`` connection is needed. A raw copy
    also preserves the exact pre-migration bytes rather than a vacuum-normalised
    equivalent: the archive is the rollback point for a schema upgrade, so it
    must be the database *as it was*, and it must not depend on the file being an
    openable/valid SQLite database at snapshot time.
    """
    destination.unlink(missing_ok=True)
    shutil.copy2(db_file, destination)


def _min_keep_for(backup_type: str) -> int:
    """Minimum backups of ``backup_type`` the prune keeps regardless of age.

    Every type honours the global :data:`MINIMUM_BACKUP_KEEP` floor. The
    ``update`` type additionally guarantees :data:`UPDATE_BACKUP_MIN_KEEP` (a
    rollback point for a schema migration must survive even past the retention
    window), so its effective floor is the larger of the two.
    """
    if backup_type == BACKUP_UPDATE:
        return max(MINIMUM_BACKUP_KEEP, UPDATE_BACKUP_MIN_KEEP)
    return MINIMUM_BACKUP_KEEP


def prune_backups(
    settings: Settings,
    backup_type: str,
    *,
    retention_days: int,
    now: datetime | None = None,
    protect: Path | None = None,
) -> list[BackupInfo]:
    """Delete backups of ``backup_type`` older than the retention window (COL-68).

    Age-based retention with a hard, count-based floor per type:

    * Backups are considered newest-first. The newest :func:`_min_keep_for`
      count are **never** pruned regardless of age -- this is the minimum-keep
      floor (:data:`MINIMUM_BACKUP_KEEP` for every type, raised to
      :data:`UPDATE_BACKUP_MIN_KEEP` for ``update``), so a long-idle instance is
      never pruned to zero and an upgrade stays rollback-able.
    * Of the remainder, only those strictly older than ``now - retention_days``
      are deleted; anything inside the window is kept.
    * ``protect`` (a just-created archive's path) is never deleted, so the
      backup that triggered the prune always survives even under clock skew.

    The floor is applied by slicing ``[keep:]`` from a newest-first list --
    which is empty (never a negative-index slice) when there are ``<= keep``
    backups -- deliberately avoiding COL-60's negative-slice bug where a
    ``[:len - keep]`` slice pruned from the *front* and deleted backups it
    should have kept.

    ``retention_days <= 0`` disables age pruning (returns ``[]``); the settings
    layer constrains the value to ``> 0`` so this is only a defensive guard.
    Returns the :class:`BackupInfo` of every archive deleted (for logging/tests).
    """
    _validate_type(backup_type)
    if retention_days <= 0:
        return []

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=retention_days)
    keep = _min_keep_for(backup_type)
    protected_name = protect.name if protect is not None else None

    # list_backups is already newest-first; keep the top `keep` unconditionally.
    of_type = [info for info in list_backups(settings) if info.type == backup_type]
    type_dir = backup_type_dir(settings, backup_type)

    deleted: list[BackupInfo] = []
    for info in of_type[keep:]:
        if info.name == protected_name:
            continue
        if info.created_at < cutoff:
            (type_dir / info.name).unlink(missing_ok=True)
            deleted.append(info)
    if deleted:
        logger.info(
            "Pruned %d stale %s backup(s) older than %d day(s)",
            len(deleted),
            backup_type,
            retention_days,
        )
    return deleted


def create_backup(
    settings: Settings,
    backup_type: str = BACKUP_MANUAL,
    *,
    retention_days: int | None = None,
    snapshot: Callable[[Path, Path], None] | None = None,
) -> BackupInfo:
    """Create one backup archive and return its :class:`BackupInfo`.

    Snapshots the live SQLite database, zips the snapshot, and atomically
    renames it into ``<data_dir>/backups/<backup_type>/``. The whole critical
    section holds :data:`_BACKUP_LOCK`; temp files are cleaned up in a
    ``finally`` so a failure leaves no partial or listable archive.

    ``snapshot`` selects how the source database is captured to the temp file.
    It defaults to :func:`_snapshot_database` (``VACUUM INTO`` -- a consistent
    copy of the *live* database, for the manual/scheduled paths). The
    pre-migration ``update`` path (COL-69) passes :func:`_raw_copy_snapshot`
    instead: it runs before the engine connects, so a byte-for-byte copy is both
    safe and the exact rollback artifact a schema upgrade needs.

    When ``retention_days`` is given (the scheduler and "Backup Now" paths pass
    the live ``backup_retention_days`` setting; the pre-migration path passes the
    schema default), :func:`prune_backups` runs after the archive is published --
    deleting older backups of *this type* past the window while honouring the
    per-type minimum-keep floor (raised to :data:`UPDATE_BACKUP_MIN_KEEP` for
    ``update``) and never touching the archive just written. A prune failure is
    logged but never fails the create: the backup already succeeded and must not
    be lost.

    Raises :class:`BackupUnavailableError` when the database isn't a file-based
    SQLite one (see :func:`resolve_sqlite_path`).
    """
    _validate_type(backup_type)
    db_file = resolve_sqlite_path(settings)
    if db_file is None:
        raise BackupUnavailableError(
            "Backups are unavailable for this database configuration "
            "(not a file-based SQLite database)."
        )

    with _BACKUP_LOCK:
        target_dir = backup_type_dir(settings, backup_type)
        target_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(UTC)
        filename = _backup_filename(now)
        final_path = target_dir / filename
        tmp_db = target_dir / f".{filename}.db.part"
        tmp_zip = target_dir / f".{filename}.part"

        snapshot_fn = snapshot or _snapshot_database
        try:
            snapshot_fn(db_file, tmp_db)
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(tmp_db, arcname=ARCHIVE_MEMBER_NAME)
            # Atomic publish: nothing matching BACKUP_FILENAME_GLOB exists until
            # this rename lands, so a partial/failed backup is never listable.
            os.replace(tmp_zip, final_path)
        finally:
            tmp_db.unlink(missing_ok=True)
            tmp_zip.unlink(missing_ok=True)

        logger.info("Wrote %s backup to %s", backup_type, final_path)
        info = _info_from_path(final_path, backup_type)

        if retention_days is not None:
            try:
                prune_backups(
                    settings,
                    backup_type,
                    retention_days=retention_days,
                    protect=final_path,
                )
            except Exception:  # noqa: BLE001 - a prune failure must not lose the backup
                logger.exception("post-backup retention prune failed for %s", backup_type)

        return info


def delete_backup(settings: Settings, backup_id: str) -> None:
    """Delete one backup archive and its file from disk (COL-65).

    The whole resolve -> floor-check -> unlink runs under :data:`_BACKUP_LOCK`
    (the same lock :func:`create_backup` holds) so the count can't shift under
    us between the guardrail check and the deletion.

    Raises:
        BackupNotFoundError: ``backup_id`` doesn't resolve to a real archive
            (unknown type, malformed/traversal id, or no file) -- the REST
            layer maps this to ``404``.
        BackupRetentionFloorError: the deletion would leave fewer than
            :data:`MINIMUM_BACKUP_KEEP` backups. Enforced *before* any file is
            touched, so a refused delete never removes anything.
    """
    with _BACKUP_LOCK:
        path = resolve_backup_path(settings, backup_id)
        if path is None:
            raise BackupNotFoundError(f"No backup archive with id={backup_id!r}")

        remaining = len(list_backups(settings)) - 1
        if remaining < MINIMUM_BACKUP_KEEP:
            raise BackupRetentionFloorError(
                f"Refusing to delete this backup: at least {MINIMUM_BACKUP_KEEP} "
                "backup must be kept so a recovery point always remains."
            )

        path.unlink()
        logger.info("Deleted backup %s", backup_id)


__all__ = [
    "ARCHIVE_MEMBER_NAME",
    "BACKUP_FILENAME_GLOB",
    "BACKUP_MANUAL",
    "BACKUP_SCHEDULED",
    "BACKUP_TYPES",
    "BACKUP_UPDATE",
    "MINIMUM_BACKUP_KEEP",
    "UPDATE_BACKUP_MIN_KEEP",
    "BackupInfo",
    "BackupNotFoundError",
    "BackupRetentionFloorError",
    "BackupUnavailableError",
    "backup_type_dir",
    "backups_root",
    "create_backup",
    "delete_backup",
    "ensure_backup_dirs",
    "is_backup_supported",
    "list_backups",
    "prune_backups",
    "resolve_backup_path",
    "resolve_sqlite_path",
]
