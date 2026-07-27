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

import logging
import os
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
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


def create_backup(settings: Settings, backup_type: str = BACKUP_MANUAL) -> BackupInfo:
    """Create one backup archive and return its :class:`BackupInfo`.

    Snapshots the live SQLite database (:func:`_snapshot_database`), zips the
    snapshot, and atomically renames it into
    ``<data_dir>/backups/<backup_type>/``. The whole critical section holds
    :data:`_BACKUP_LOCK`; temp files are cleaned up in a ``finally`` so a
    failure leaves no partial or listable archive.

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

        try:
            _snapshot_database(db_file, tmp_db)
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(tmp_db, arcname=ARCHIVE_MEMBER_NAME)
            # Atomic publish: nothing matching BACKUP_FILENAME_GLOB exists until
            # this rename lands, so a partial/failed backup is never listable.
            os.replace(tmp_zip, final_path)
        finally:
            tmp_db.unlink(missing_ok=True)
            tmp_zip.unlink(missing_ok=True)

        logger.info("Wrote %s backup to %s", backup_type, final_path)
        return _info_from_path(final_path, backup_type)


__all__ = [
    "ARCHIVE_MEMBER_NAME",
    "BACKUP_FILENAME_GLOB",
    "BACKUP_MANUAL",
    "BACKUP_SCHEDULED",
    "BACKUP_TYPES",
    "BACKUP_UPDATE",
    "BackupInfo",
    "BackupUnavailableError",
    "backup_type_dir",
    "backups_root",
    "create_backup",
    "ensure_backup_dirs",
    "is_backup_supported",
    "list_backups",
    "resolve_sqlite_path",
]
