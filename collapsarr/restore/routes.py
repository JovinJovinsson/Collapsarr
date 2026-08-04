"""HTTP REST endpoint to restore the database from a listed backup (COL-71).

One route, mounted under ``/api/system`` alongside :mod:`collapsarr.backup.routes`
(same prefix, kept in its own router/module because it belongs to the restore
domain -- it is the producer that arms the marker
:mod:`collapsarr.restore.marker` defines and :mod:`collapsarr.restore.engine`
consumes on the next boot):

* ``POST /api/system/backup/restore/{id}`` -- runs the validate-before-stage
  gate (:func:`collapsarr.restore.request.stage_restore`) against the named
  backup. A gate failure returns ``422`` with a clear message and touches
  neither the marker nor the running database. An unknown ``id`` returns
  ``404`` (same resolution as the download/delete routes). On success the
  database is staged and the marker written, then this process's own shutdown
  is triggered (:func:`trigger_shutdown`) so the supervisor (Docker/systemd)
  restarts it and the swap applies on next boot -- the endpoint itself never
  swaps anything into the live database.

Because everything under ``/api`` is gated by the auth middleware (session
cookie or API key; see :mod:`collapsarr.auth.enforcement`), this route
inherits that gate with no per-route wiring, same as every other endpoint
under this prefix.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..backup.service import BackupNotFoundError
from ..config import Settings
from . import upload
from .request import RestoreGateError, stage_restore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])


# --- schemas -----------------------------------------------------------------


class RestoreAccepted(BaseModel):
    """Response body for a successfully staged restore."""

    status: str
    backup_id: str


class RestoreUploadAccepted(BaseModel):
    """Response body for a successfully staged restore from an uploaded archive.

    No ``backup_id`` -- an uploaded archive isn't a listed backup on disk, so
    there is no id to echo back; ``status`` alone mirrors the listed-restore
    ``202`` shape enough for the UI's "restarting" transition.
    """

    status: str


# --- shutdown trigger ----------------------------------------------------------


def trigger_shutdown(delay: float = 0.25) -> None:
    """Terminate this process shortly after the HTTP response is sent.

    The short delay runs off a background timer (not the request thread) so
    the ASGI server has a moment to flush the ``202`` response to the client
    before the process dies mid-response.

    The exit code is deliberately **non-zero**. This repo documents two
    supervised deployment shapes (see ``README.md``'s "Running on startup"):
    Docker's ``restart: unless-stopped`` (restarts on *any* exit) and a
    systemd unit with ``Restart=on-failure`` (restarts only on a non-zero
    exit or a signal -- a clean ``0`` exit would **not** trigger a restart
    there). A non-zero exit is restarted by both, so it's the one choice that
    reliably hands control back to the supervisor either way. An install with
    no supervisor at all simply stays down -- documented in the UI's restore
    confirmation copy as "restart it yourself if this install isn't
    supervised."

    ``os._exit`` (not ``sys.exit``) skips Python's normal interpreter
    teardown (atexit hooks, ``finally`` blocks, the app lifespan's own
    shutdown code) -- appropriate here because this is a deliberate,
    unconditional process kill: everything that must survive the restart (the
    staged database, the restore marker) was already durably written to disk
    by :func:`~collapsarr.restore.request.stage_restore` before this is ever
    called, so there is nothing left to flush.

    Tests never let this actually run -- they monkeypatch
    ``collapsarr.restore.routes.trigger_shutdown`` to a no-op, the same way
    :mod:`tests.test_restore_engine` monkeypatches ``create_backup`` to
    simulate a transient failure without touching real I/O.
    """
    threading.Timer(delay, lambda: os._exit(1)).start()


# --- helpers -------------------------------------------------------------------


def _settings(request: Request) -> Settings:
    """Return the app's resolved settings (set on ``app.state`` by the factory)."""
    settings: Settings = request.app.state.settings
    return settings


# --- endpoints -----------------------------------------------------------------

#: Temp filename the streaming upload is spooled to under ``<data_dir>``,
#: dot-prefixed like the codebase's other internal artifacts. Removed in a
#: ``finally`` on every path, so a failed/oversized upload leaves nothing behind.
UPLOAD_TMP_FILENAME = ".restore_upload.zip.part"


@router.post(
    "/backup/restore/upload",
    status_code=202,
    response_model=RestoreUploadAccepted,
)
async def restore_upload_endpoint(request: Request) -> RestoreUploadAccepted:
    """Stage a restore from an *uploaded* archive and shut down to apply it.

    The request body is the raw ``.zip`` bytes (the frontend POSTs the chosen
    file directly). They are streamed to a temp file, aborting the moment the
    running total exceeds :data:`~collapsarr.restore.upload.MAX_UPLOAD_ARCHIVE_BYTES`
    so an oversized upload can never be spooled unbounded to disk, then handed to
    :func:`~collapsarr.restore.upload.stage_restore_from_upload` for hardened
    extraction and the *same* validate -> stage -> marker gate as the
    listed-backup restore (SQLite + ``global_settings`` sentinel +
    version-compatibility guard).

    This route is declared **before** ``POST /backup/restore/{backup_id:path}``
    so the literal ``upload`` segment matches here rather than being captured as
    a backup id.

    * ``413`` -- the upload exceeded the max archive size. No marker written.
    * ``422`` -- the archive failed hardened extraction (bad zip, zip-slip,
      missing/oversize DB entry) or the validate gate (not SQLite, missing
      sentinel table, or a newer-than-supported revision). No marker written and
      the running database is untouched.
    * ``202`` -- the database was staged and the marker armed; this process is
      now shutting down so the supervisor restarts it and the swap applies on
      next boot.

    Auth is inherited from the ``/api`` middleware, same as every other route
    under this prefix.
    """
    settings = _settings(request)
    upload_tmp = Path(settings.data_dir).expanduser() / UPLOAD_TMP_FILENAME
    try:
        upload_tmp.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = upload.MAX_UPLOAD_ARCHIVE_BYTES
        received = 0
        try:
            with upload_tmp.open("wb") as dst:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > max_bytes:
                        raise upload.UploadTooLargeError(
                            "The uploaded archive exceeds the maximum allowed "
                            f"size of {max_bytes} bytes."
                        )
                    dst.write(chunk)
            if received == 0:
                raise RestoreGateError(
                    "No uploaded archive was received in the request body."
                )
            upload.stage_restore_from_upload(settings, upload_tmp)
        except upload.UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except RestoreGateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        upload_tmp.unlink(missing_ok=True)

    logger.warning(
        "Restore staged from an uploaded archive; shutting down so the "
        "supervisor restarts this instance and the swap applies on next boot.",
    )
    trigger_shutdown()
    return RestoreUploadAccepted(status="restoring")


@router.post(
    "/backup/restore/{backup_id:path}",
    status_code=202,
    response_model=RestoreAccepted,
)
def restore_backup_endpoint(backup_id: str, request: Request) -> RestoreAccepted:
    """Stage a restore from backup ``backup_id`` and shut down to apply it.

    ``backup_id`` is the ``<type>/<filename>`` id from
    :func:`~collapsarr.backup.service.list_backups` (the ``:path`` converter
    lets it carry its embedded ``/``, same as the download/delete routes).

    * ``404`` -- ``backup_id`` doesn't resolve to a real archive.
    * ``422`` -- the archive failed the validate-before-stage gate (not a
      valid zip / missing DB entry, not a valid SQLite file, or missing the
      ``global_settings`` sentinel table). Neither the marker nor the running
      database is touched.
    * ``202`` -- the database was staged and the marker armed; this process is
      now shutting down so the supervisor restarts it and the swap applies on
      next boot.
    """
    settings = _settings(request)
    try:
        stage_restore(settings, backup_id)
    except BackupNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RestoreGateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.warning(
        "Restore staged from backup %s; shutting down so the supervisor "
        "restarts this instance and the swap applies on next boot.",
        backup_id,
    )
    trigger_shutdown()
    return RestoreAccepted(status="restoring", backup_id=backup_id)


__all__ = ["UPLOAD_TMP_FILENAME", "router", "trigger_shutdown"]
