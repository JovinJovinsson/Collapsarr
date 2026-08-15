"""HTTP endpoint for the opt-in FFmpeg auto-download trigger (COL-222, ADR 0002).

Thin layer over :mod:`collapsarr.ffmpeg_download.service`, exposed as a
FastAPI :class:`~fastapi.APIRouter` mounted under ``/api/system`` by
:func:`collapsarr.main.create_app`, matching
:mod:`collapsarr.update_check.routes`'/:mod:`collapsarr.health.routes`'s
shape. Everything under ``/api`` is gated by the API-key/session auth
middleware (:mod:`collapsarr.auth.enforcement`), so no per-route auth wiring
is needed here.

Endpoint:

* ``POST /api/system/ffmpeg/download`` -- downloads the pinned, checksum-
  verified FFmpeg build for this platform (COL-217), extracts it into
  ``<data_dir>/ffmpeg/``, and persists the resolved path onto
  ``GlobalSettings.ffmpeg_path`` (COL-218). Gated to ``install_method !=
  "docker"`` (COL-215) -- Docker already bundles FFmpeg into the image, so
  this endpoint ``403``s under a Docker install rather than silently
  no-oping; the frontend hides the triggering button entirely for Docker
  installs (``HealthBanner.tsx``), matching ADR 0002's "available to any
  non-Docker install method" decision. Never triggered automatically (ADR
  0001/0002) -- this only ever runs when an operator explicitly calls it.

On any failure (offline, source unreachable, checksum mismatch, a malformed
or ffmpeg-less archive, or no pinned build for this platform), responds
``502`` with a human-readable ``detail`` and persists nothing -- see
:mod:`collapsarr.ffmpeg_download.service`'s module docstring for why
``ffmpeg_path`` can never end up holding a partial/unverified value. The
caller can simply retry by calling this endpoint again.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import Settings
from ..database import get_session
from ..system.info import INSTALL_METHOD_DOCKER, install_method
from .service import download_and_install_ffmpeg

router = APIRouter(prefix="/api/system/ffmpeg", tags=["system"])


# --- schemas -----------------------------------------------------------------


class FfmpegDownloadRead(BaseModel):
    """Response shape for a successful ``POST /api/system/ffmpeg/download``."""

    ffmpeg_path: str


# --- endpoints ---------------------------------------------------------------


@router.post("/download", response_model=FfmpegDownloadRead)
def download_ffmpeg_endpoint(
    request: Request,
    session: Session = Depends(get_session),
) -> FfmpegDownloadRead:
    """Download, verify, extract, and persist FFmpeg for this install (COL-222).

    ``403`` under a Docker install (FFmpeg is already bundled in the image --
    see the module docstring). Otherwise delegates the whole download-verify-
    extract-persist flow to :func:`~collapsarr.ffmpeg_download.service.
    download_and_install_ffmpeg`; a failed outcome is surfaced as a ``502``
    carrying that failure's own human-readable ``error`` as the response
    ``detail``, so the frontend's ``apiErrorMessage`` helper renders it
    directly. ``ffmpeg_download_transport`` -- forwarded from
    :func:`collapsarr.main.create_app` onto ``app.state`` -- lets tests inject
    an ``httpx.MockTransport`` instead of a real network call; production
    leaves it unset.
    """
    if install_method() == INSTALL_METHOD_DOCKER:
        raise HTTPException(
            status_code=403,
            detail=(
                "FFmpeg auto-download is not available for Docker installs; "
                "FFmpeg is already bundled in the image."
            ),
        )

    settings: Settings = request.app.state.settings
    transport = getattr(request.app.state, "ffmpeg_download_transport", None)
    outcome = download_and_install_ffmpeg(
        session, data_dir=settings.data_dir, transport=transport
    )
    if not outcome.ok or outcome.ffmpeg_path is None:
        raise HTTPException(status_code=502, detail=outcome.error)

    return FfmpegDownloadRead(ffmpeg_path=outcome.ffmpeg_path)


__all__ = ["FfmpegDownloadRead", "router"]
