"""Database backup module (COL-63).

The tracer-bullet slice of the Radarr-parity backup system (Epic COL-61): a
service that snapshots the live SQLite database into a zip under
``<data_dir>/backups/{manual,scheduled,update}/`` and the REST skeleton that
drives it (:mod:`collapsarr.backup.routes`, mounted under ``/api/system``).

Only the ``manual`` ("Backup Now") path is wired here; the ``scheduled`` and
``update`` on-disk layout and the :data:`~collapsarr.backup.service.BACKUP_TYPES`
seam are established now so the later slices (download, delete, retention,
scheduling, pre-migration fold) build on them without re-shaping this module.
"""

from __future__ import annotations

from .service import (
    BACKUP_MANUAL,
    BACKUP_SCHEDULED,
    BACKUP_TYPES,
    BACKUP_UPDATE,
    MINIMUM_BACKUP_KEEP,
    UPDATE_BACKUP_MIN_KEEP,
    BackupInfo,
    BackupNotFoundError,
    BackupRetentionFloorError,
    BackupUnavailableError,
    backup_type_dir,
    backups_root,
    create_backup,
    delete_backup,
    ensure_backup_dirs,
    is_backup_supported,
    list_backups,
    prune_backups,
    resolve_backup_path,
    resolve_sqlite_path,
)

__all__ = [
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
