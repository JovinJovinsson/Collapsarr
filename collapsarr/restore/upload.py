"""Restore from an uploaded archive: the untrusted-input surface (COL-73).

The sibling of :mod:`collapsarr.restore.request`. Where that module stages a
restore from a backup this instance produced itself (resolved out of the app's
own ``backups/`` directory), this one stages a restore from an archive an
operator *uploads* -- typically one they downloaded earlier and are now
restoring onto a fresh box whose ``backups/`` folder is gone. The bytes are
therefore untrusted, so the extraction here is hardened well past what the
trusted-directory path needs:

#. **Bounded raw upload** -- the HTTP layer
   (:func:`collapsarr.restore.routes.restore_upload_endpoint`) streams the
   request body to a temp file and aborts the moment it exceeds
   :data:`MAX_UPLOAD_ARCHIVE_BYTES`, so a malicious client can never make the
   server buffer an unbounded upload to disk.
#. **Zip-slip rejection** -- every entry in the archive is checked
   (:func:`collapsarr.archive_safety.is_unsafe_archive_entry_path`, shared
   with :mod:`collapsarr.ffmpeg_download.service`'s own archive extraction,
   COL-222) and the whole upload is rejected if *any* entry is an absolute
   path, carries a drive/UNC prefix, or contains a ``..`` traversal
   component. This is defence-in-depth: the single member actually extracted
   is addressed by its fixed name (:data:`ARCHIVE_MEMBER_NAME`, no
   separators), but a hostile archive is refused outright rather than merely
   ignored.
#. **Decompression-bomb cap** -- only the single expected DB entry is
   extracted, and it is streamed out through a hard
   :data:`MAX_UNCOMPRESSED_BYTES` ceiling (both the zip's declared size and the
   actual decompressed byte count are checked, so a lying header can't slip a
   bomb past), so a small archive can never inflate into an unbounded write.

Once the bytes are safely on disk as a staging file, the flow rejoins the
listed-backup pipeline exactly: :func:`collapsarr.restore.request.validate_staged_db`
runs the same SQLite + ``global_settings`` sentinel + version-compatibility
(COL-72) gate, and only on success is the file promoted to the staging path and
the restore marker written. No marker or staged database is ever produced until
the full gate passes, so an oversized, malformed, or newer-revision upload
leaves the running instance completely untouched.
"""

from __future__ import annotations

import logging
import os
import zipfile
from pathlib import Path

from collapsarr.archive_safety import is_unsafe_archive_entry_path
from collapsarr.backup.service import ARCHIVE_MEMBER_NAME
from collapsarr.config import Settings
from collapsarr.restore.marker import write_restore_marker
from collapsarr.restore.request import (
    RestoreGateError,
    restore_staging_path,
    validate_staged_db,
)

logger = logging.getLogger(__name__)

#: Hard ceiling on the *raw* uploaded archive, enforced by the streaming HTTP
#: endpoint before the bytes are ever handed here. Generous enough for a real
#: Collapsarr database snapshot (a metadata DB, not media) while capping the
#: amount an unauthenticated-then-authenticated client can spool to disk.
MAX_UPLOAD_ARCHIVE_BYTES = 512 * 1024 * 1024

#: Hard ceiling on the *uncompressed* database entry pulled out of the archive.
#: Set well above the raw cap so a highly-compressible-but-legitimate SQLite
#: file still fits, but bounded so a decompression bomb can't inflate a small
#: upload into an unbounded write. Enforced on both the declared and the actual
#: decompressed size.
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024

#: Streaming chunk size for the bounded extract.
_CHUNK_SIZE = 1024 * 1024


class UploadTooLargeError(RuntimeError):
    """The uploaded archive exceeded :data:`MAX_UPLOAD_ARCHIVE_BYTES`.

    Raised by the streaming HTTP endpoint (not this module) while spooling the
    request body; kept here so the cap and the error that enforces it live
    together. The REST layer maps it to a ``413``. No staging file or marker is
    written -- the upload is refused before extraction even begins.
    """


def _extract_upload_db_member(archive_path: Path, dest_path: Path) -> None:
    """Safely extract the single DB entry from an untrusted archive to ``dest_path``.

    Covers the hardened-extraction half of the gate: zip validity, zip-slip
    rejection across *all* entries, presence of the expected
    :data:`ARCHIVE_MEMBER_NAME` entry, and a decompression-bomb ceiling applied
    to both the declared and the actually-decompressed size. Only the one
    expected member is ever extracted. Raises :class:`RestoreGateError` on any
    failure (the REST layer maps it to ``422``); ``dest_path`` may hold a
    partial write on a raise and is the caller's to clean up.
    """
    # Read the cap through the module global at call time so tests can lower it
    # via monkeypatch.
    max_uncompressed = MAX_UNCOMPRESSED_BYTES
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if is_unsafe_archive_entry_path(info.filename):
                    raise RestoreGateError(
                        "The uploaded archive contains an unsafe entry path "
                        f"({info.filename!r}); it looks like a path-traversal "
                        "(zip-slip) attempt and was rejected."
                    )
            if ARCHIVE_MEMBER_NAME not in archive.namelist():
                raise RestoreGateError(
                    "The uploaded archive does not contain the expected "
                    f"'{ARCHIVE_MEMBER_NAME}' database entry."
                )
            member = archive.getinfo(ARCHIVE_MEMBER_NAME)
            if member.file_size > max_uncompressed:
                raise RestoreGateError(
                    "The uploaded archive's database entry is larger than the "
                    f"maximum allowed uncompressed size of {max_uncompressed} bytes."
                )
            written = 0
            with archive.open(member) as source, dest_path.open("wb") as dst:
                while True:
                    chunk = source.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_uncompressed:
                        raise RestoreGateError(
                            "The uploaded archive's database entry exceeds the "
                            f"maximum allowed uncompressed size of {max_uncompressed} "
                            "bytes; it may be a decompression bomb and was rejected."
                        )
                    dst.write(chunk)
    except zipfile.BadZipFile as exc:
        raise RestoreGateError("The uploaded archive is not a valid zip file.") from exc


def stage_restore_from_upload(settings: Settings, archive_path: Path) -> Path:
    """Stage a restore from an uploaded archive at ``archive_path`` and arm the marker.

    Runs the hardened extraction (:func:`_extract_upload_db_member`) followed by
    the shared post-extraction gate
    (:func:`collapsarr.restore.request.validate_staged_db`) -- the *same*
    SQLite + sentinel + version-compatibility (COL-72) checks the listed-backup
    restore runs. Only once the whole gate passes are the validated bytes
    atomically promoted to :func:`~collapsarr.restore.request.restore_staging_path`
    and the restore marker written, so a rejected upload never touches the
    running database. Returns the staging path.

    ``archive_path`` is the temp file the streaming endpoint already wrote (and
    already size-capped); this function does not own it and never deletes it.

    Raises:
        RestoreGateError: the archive failed hardened extraction (bad zip,
            zip-slip, missing DB entry, oversize uncompressed) or the shared
            post-extraction gate -- no marker written, running database
            untouched. The REST layer maps this to ``422``.
    """
    staged_path = restore_staging_path(settings)
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = staged_path.with_name(f".{staged_path.name}.part")
    try:
        _extract_upload_db_member(archive_path, tmp_path)
        validate_staged_db(settings, tmp_path)
        # Only reached once the full gate has passed: atomically promote the
        # validated temp file so a reader never observes a partial/unvalidated
        # staged database.
        os.replace(tmp_path, staged_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    write_restore_marker(settings, staged_path)
    logger.warning(
        "Restore staged from an uploaded archive -> %s; armed for the next boot.",
        staged_path,
    )
    return staged_path


__all__ = [
    "MAX_UNCOMPRESSED_BYTES",
    "MAX_UPLOAD_ARCHIVE_BYTES",
    "UploadTooLargeError",
    "stage_restore_from_upload",
]
