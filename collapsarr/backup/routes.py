"""HTTP REST endpoints for database backups (COL-63).

Thin layer over :mod:`collapsarr.backup.service`, exposed as a FastAPI
:class:`~fastapi.APIRouter` mounted under ``/api/system`` by
:func:`collapsarr.main.create_app`. Because everything under ``/api`` is gated
by the auth middleware (session cookie or API key; see
:mod:`collapsarr.auth.enforcement`), both routes here inherit that gate -- no
per-route auth wiring is needed.

Four endpoints:

* ``GET /api/system/backup`` -- returns ``{supported, backups}``. ``supported``
  is ``false`` when the database isn't file-based SQLite, which is how the UI
  decides between showing the backup controls and the "unavailable for this
  database configuration" state. ``backups`` lists every finished archive
  (id, name, type, size, created_at), newest first.
* ``POST /api/system/backup`` -- creates a ``manual`` backup now and returns its
  summary with ``202 Accepted``. A ``409`` is returned when backups are
  unavailable for the current database configuration.
* ``GET /api/system/backup/{id}/download`` (COL-64) -- streams the archive off
  disk as a ``.zip`` download. This is the *only* fetch path for a backup
  archive: ``<data_dir>/backups/`` is never exposed via a static file mount
  (see :mod:`collapsarr.frontend`, which only ever mounts the built frontend's
  own asset directory), so every download goes through this auth-gated route.
  An unknown/invalid ``id`` -- wrong type, a filename that doesn't match a real
  archive, or a path-traversal attempt -- is rejected with ``404`` rather than
  distinguishing why (see :func:`~collapsarr.backup.service.resolve_backup_path`).
* ``DELETE /api/system/backup/{id}`` (COL-65) -- removes one backup and its file
  from disk, returning ``204``. An unknown/invalid ``id`` is a ``404`` (same
  resolution as download); a delete that would drop below the minimum-keep floor
  (:data:`~collapsarr.backup.service.MINIMUM_BACKUP_KEEP`) is refused with a
  ``409`` and a clear message, without touching any file. Full age-based
  retention is a separate slice (COL-68); this endpoint only enforces the floor
  as the manual-deletion guardrail.

The JSON uses the codebase's snake_case convention (``created_at``), matching
every other endpoint (settings, jobs history).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import Settings
from .service import (
    BACKUP_MANUAL,
    BackupInfo,
    BackupNotFoundError,
    BackupRetentionFloorError,
    BackupUnavailableError,
    create_backup,
    delete_backup,
    is_backup_supported,
    list_backups,
    resolve_backup_path,
)

router = APIRouter(prefix="/api/system", tags=["system"])


# --- schemas -----------------------------------------------------------------


class BackupRead(BaseModel):
    """Summary of a single backup archive."""

    id: str
    name: str
    type: str
    size: int
    created_at: datetime


class BackupListRead(BaseModel):
    """List response: whether backups are supported here, plus the archives."""

    supported: bool
    backups: list[BackupRead]


def _to_read(info: BackupInfo) -> BackupRead:
    return BackupRead(
        id=info.id,
        name=info.name,
        type=info.type,
        size=info.size,
        created_at=info.created_at,
    )


def _settings(request: Request) -> Settings:
    """Return the app's resolved settings (set on ``app.state`` by the factory)."""
    settings: Settings = request.app.state.settings
    return settings


# --- endpoints ---------------------------------------------------------------


@router.get("/backup", response_model=BackupListRead)
def list_backups_endpoint(request: Request) -> BackupListRead:
    """List existing backups and whether the database supports backing up."""
    settings = _settings(request)
    return BackupListRead(
        supported=is_backup_supported(settings),
        backups=[_to_read(info) for info in list_backups(settings)],
    )


@router.post("/backup", status_code=202, response_model=BackupRead)
def create_backup_endpoint(request: Request) -> BackupRead:
    """Create a ``manual`` backup now and return its summary (``202``).

    Returns ``409`` when backups are unavailable for the current database
    configuration (non-file-based SQLite).
    """
    settings = _settings(request)
    try:
        info = create_backup(settings, BACKUP_MANUAL)
    except BackupUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_read(info)


@router.get("/backup/{backup_id:path}/download")
def download_backup_endpoint(backup_id: str, request: Request) -> FileResponse:
    """Stream a backup archive as a ``.zip`` download.

    ``backup_id`` is the ``<type>/<filename>`` id from :func:`list_backups`
    (the ``:path`` converter lets it carry its embedded ``/``).
    :func:`~collapsarr.backup.service.resolve_backup_path` maps every invalid
    shape -- unknown type, malformed/traversal filename, or no file on disk --
    to ``None``, which this route turns into a ``404``.
    """
    settings = _settings(request)
    path = resolve_backup_path(settings, backup_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"No backup archive with id={backup_id!r}")
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.delete("/backup/{backup_id:path}", status_code=204)
def delete_backup_endpoint(backup_id: str, request: Request) -> None:
    """Delete one backup and its file from disk (COL-65).

    ``backup_id`` is the ``<type>/<filename>`` id from :func:`list_backups`
    (the ``:path`` converter lets it carry its embedded ``/``). Maps the
    service's two refusal modes to distinct HTTP statuses:

    * an unknown/invalid id (unknown type, malformed/traversal filename, or no
      file on disk) -> ``404`` -- indistinguishable from a missing file, same
      as the download route;
    * a delete that would breach the minimum-keep floor
      (:data:`~collapsarr.backup.service.MINIMUM_BACKUP_KEEP`) -> ``409`` with
      the guardrail message, *without* touching any file.

    On success returns ``204`` with no body.
    """
    settings = _settings(request)
    try:
        delete_backup(settings, backup_id)
    except BackupNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BackupRetentionFloorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
