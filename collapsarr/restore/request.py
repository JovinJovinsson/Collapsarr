"""Restore-request staging: the validate-before-stage gate (COL-71).

The producer half of the restore flow, sitting opposite the boot-time consumer
in :mod:`collapsarr.restore.engine`. :func:`stage_restore` turns a listed
backup id into an armed restore: it extracts the archive's database entry to
a staging file, runs the **validate-before-stage gate** against it, and only
on success writes the restore marker (:mod:`collapsarr.restore.marker`) that
the next boot's swap engine consumes.

The gate exists so a corrupt or foreign zip can never brick the next boot: a
marker is only ever written for a file already proven to be a real,
identifiable Collapsarr database. Three checks, in order:

#. **Valid zip containing the expected DB entry** -- ``backup_id`` resolves to
   a real archive on disk (see :func:`~collapsarr.backup.service.resolve_backup_path`)
   and it opens as a zip carrying :data:`~collapsarr.backup.service.ARCHIVE_MEMBER_NAME`.
#. **Extracted file opens as valid SQLite** -- the member's bytes, once
   written to a temp file, both look like a SQLite file (the same header check
   :func:`collapsarr.restore.engine._is_sqlite_file` uses at swap time) and
   actually open via ``sqlite3``.
#. **Carries the ``global_settings`` sentinel table** -- reuses
   :data:`collapsarr.migrations.SENTINEL_TABLE`, the same "is this really a
   populated Collapsarr database" signal ``upgrade_to_head`` uses to decide
   whether an unversioned database is adoptable. A file that opens as SQLite
   but isn't a Collapsarr database at all (or is truly empty) fails here.

Any gate failure raises :class:`RestoreGateError` with a message safe to
surface directly to the operator; the temp file is cleaned up and neither the
staging path nor the restore marker is touched. :func:`stage_restore` also
raises :class:`~collapsarr.backup.service.BackupNotFoundError` unchanged when
``backup_id`` doesn't resolve to a real archive, so callers handle "unknown
id" and "gate failure" as the two distinct outcomes the REST layer maps to
404 and 422 respectively.

**Stage-gate seam**: the version-compatibility guard (COL-72, a separate
later ticket) slots in as a fourth check inside this same gate -- after
:func:`_open_and_check_sentinel` confirms the file is a real Collapsarr
database, a revision check can read its ``alembic_version`` and reject a
newer-than-supported schema before the temp file is ever promoted to the
staging path. Nothing about this function's shape needs to change for that;
it is deliberately one gate function with checks run in sequence, each raising
the same :class:`RestoreGateError` on failure.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import zipfile
from pathlib import Path

from collapsarr.backup.service import (
    ARCHIVE_MEMBER_NAME,
    BackupNotFoundError,
    resolve_backup_path,
)
from collapsarr.config import Settings
from collapsarr.migrations import SENTINEL_TABLE
from collapsarr.restore.engine import _is_sqlite_file
from collapsarr.restore.marker import write_restore_marker

logger = logging.getLogger(__name__)

#: Filename of the staged database under ``<data_dir>``, dot-prefixed like the
#: rest of the codebase's internal/temp artifacts (the backup service's
#: ``.part`` temp files, the marker's own dot-prefixed atomic-write temp).
#: A restore request always (re)writes this same path -- a second request
#: before the first has been consumed by a boot simply overwrites it, and the
#: marker is rewritten to match, so "last request wins" with no orphaned files.
RESTORE_STAGED_FILENAME = ".restore_staged.db"


class RestoreGateError(RuntimeError):
    """The validate-before-stage gate rejected a restore request.

    Raised by :func:`stage_restore` for any of the three gate failures (not a
    valid zip / missing DB entry, not a valid SQLite file, missing the
    ``global_settings`` sentinel table). The message is written to be shown to
    the operator as-is. The REST layer (:mod:`collapsarr.restore.routes`) maps
    this to a ``422``. No marker is ever written and the running instance's
    database is never touched on this path.
    """


def restore_staging_path(settings: Settings) -> Path:
    """Return the staging path (``<data_dir>/.restore_staged.db``) for a restore.

    Not created here -- :func:`stage_restore` is the only writer, and the
    boot-time swap engine (:func:`collapsarr.restore.engine.apply_pending_restore`)
    deletes the file once it has consumed it.
    """
    return Path(settings.data_dir).expanduser() / RESTORE_STAGED_FILENAME


def _extract_db_member(archive_path: Path) -> bytes:
    """Return the raw bytes of the archive's DB entry, or raise :class:`RestoreGateError`.

    Covers gate check #1: the archive must open as a zip and contain
    :data:`~collapsarr.backup.service.ARCHIVE_MEMBER_NAME`.
    """
    try:
        with zipfile.ZipFile(archive_path) as archive:
            if ARCHIVE_MEMBER_NAME not in archive.namelist():
                raise RestoreGateError(
                    f"The backup archive does not contain the expected "
                    f"'{ARCHIVE_MEMBER_NAME}' database entry."
                )
            return archive.read(ARCHIVE_MEMBER_NAME)
    except zipfile.BadZipFile as exc:
        raise RestoreGateError("The backup archive is not a valid zip file.") from exc


def _open_and_check_sentinel(path: Path) -> None:
    """Validate ``path`` opens as SQLite and carries the sentinel table.

    Covers gate checks #2 and #3. The header check runs first (cheap, and
    rejects obvious garbage without letting ``sqlite3`` anywhere near a
    non-database file); the ``sqlite3`` connection that follows is both the
    "opens as valid SQLite" proof and the vehicle for the sentinel-table
    query, so a single open serves both checks.
    """
    if not _is_sqlite_file(path):
        raise RestoreGateError(
            "The backup's database file is not a valid SQLite database."
        )
    try:
        # A plain path connection (not a read-only URI) mirrors the codebase's
        # other sqlite3.connect(str(...)) call sites (e.g. _snapshot_database)
        # and sidesteps file: URI escaping for paths with unusual characters.
        # This is our own just-extracted temp file, never touched again after
        # this check, so the lack of an OS-level read-only guard is immaterial.
        connection = sqlite3.connect(str(path))
        try:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (SENTINEL_TABLE,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise RestoreGateError(
            f"The backup's database file could not be opened as SQLite: {exc}"
        ) from exc
    if row is None:
        raise RestoreGateError(
            f"The backup's database is missing the '{SENTINEL_TABLE}' table; "
            "it does not look like a Collapsarr database."
        )


def stage_restore(settings: Settings, backup_id: str) -> Path:
    """Stage a restore from the listed backup ``backup_id`` and arm the marker.

    Runs the validate-before-stage gate (see module docstring) against the
    archive's database entry. On success, the validated bytes are promoted
    (atomic rename) to :func:`restore_staging_path` and the restore marker is
    written pointing at it -- the same seam
    :func:`~collapsarr.restore.marker.write_restore_marker` documents as the
    hand-off to the boot-time swap engine. Returns the staging path.

    Raises:
        BackupNotFoundError: ``backup_id`` doesn't resolve to a real archive
            (unknown type, malformed/traversal id, or no file) -- unchanged
            from :func:`~collapsarr.backup.service.resolve_backup_path`, so
            callers can map it the same way the download/delete routes do
            (``404``).
        RestoreGateError: the gate rejected the archive -- no marker is
            written and the running instance's database is untouched.
    """
    archive_path = resolve_backup_path(settings, backup_id)
    if archive_path is None:
        raise BackupNotFoundError(f"No backup archive with id={backup_id!r}")

    data = _extract_db_member(archive_path)

    staged_path = restore_staging_path(settings)
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = staged_path.with_name(f".{staged_path.name}.part")
    try:
        tmp_path.write_bytes(data)
        _open_and_check_sentinel(tmp_path)
        # Only reached once the gate has fully passed: atomically promote the
        # validated temp file to the staging path so a reader never observes
        # a partially-written or not-yet-validated staged database.
        os.replace(tmp_path, staged_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    write_restore_marker(settings, staged_path)
    logger.warning(
        "Restore staged from backup %s -> %s; armed for the next boot.",
        backup_id,
        staged_path,
    )
    return staged_path


__all__ = [
    "RESTORE_STAGED_FILENAME",
    "RestoreGateError",
    "restore_staging_path",
    "stage_restore",
]
