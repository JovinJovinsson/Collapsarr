"""HTTP REST endpoint for the wanted-list (COL-28).

Thin GET layer over :mod:`collapsarr.media.service`, exposed as a FastAPI
:class:`~fastapi.APIRouter` mounted under ``/api`` by
:func:`collapsarr.main.create_app`. Everything under ``/api`` is gated by the
API-key middleware (COL-26), so this route inherits key-based auth with no
per-route wiring.

The "wanted-list" is every tracked file missing at least one *currently
enabled* target -- the same notion Sonarr/Radarr's ``/wanted/missing`` view
expresses. Which targets count as enabled is read live from the persisted
:class:`~collapsarr.settings.models.GlobalSettings` row rather than re-derived
from stored status rows, so a settings change is reflected immediately (a
target no longer enabled stops appearing as "wanted" without every file needing
a rescan first). Each returned file carries the exact ``(language, target)``
pairs still missing, which is the granularity the downmix job queue and a
future Wanted UI both need.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_session
from ..downmix.targets import DownmixTarget
from ..settings.service import as_downmix_settings, get_global_settings
from .models import MediaTargetStatus
from .service import list_files_missing_targets, list_target_statuses

router = APIRouter(prefix="/api", tags=["wanted"])


# --- schemas -----------------------------------------------------------------


class WantedTarget(BaseModel):
    """One ``(language, target)`` pair still missing on a wanted file."""

    language: str
    target: DownmixTarget


class WantedFile(BaseModel):
    """A tracked file missing at least one enabled target, with those pairs."""

    id: int
    file_path: str
    missing_targets: list[WantedTarget]
    created_at: datetime
    updated_at: datetime


# --- endpoints ---------------------------------------------------------------


@router.get("/wanted", response_model=list[WantedFile])
def list_wanted_endpoint(session: Session = Depends(get_session)) -> list[WantedFile]:
    """List tracked files missing at least one currently-enabled downmix target.

    Enabled targets are read from the global settings row; each file's still
    ``missing`` ``(language, target)`` pairs (restricted to those enabled
    targets) are attached. Ordered by file id (insertion order), matching the
    service layer.
    """
    enabled_targets = as_downmix_settings(get_global_settings(session)).enabled_targets
    files = list_files_missing_targets(session, enabled_targets=enabled_targets)

    result: list[WantedFile] = []
    for media in files:
        missing = [
            WantedTarget(language=status.language, target=status.target)
            for status in list_target_statuses(session, media.file_path)
            if status.status == MediaTargetStatus.MISSING and status.target in enabled_targets
        ]
        result.append(
            WantedFile(
                id=media.id,
                file_path=media.file_path,
                missing_targets=missing,
                created_at=media.created_at,
                updated_at=media.updated_at,
            )
        )
    return result
