"""Startup health checks -- FFmpeg availability (COL-38).

:func:`check_ffmpeg` is a lightweight presence check run once at app startup
(wired into :func:`collapsarr.main.create_app`'s lifespan): rather than let a
missing FFmpeg binary surface as a cryptic mid-job failure deep in
:mod:`collapsarr.downmix.remux` (whose own ``ffmpeg_path`` default,
``"ffmpeg"``, is resolved via ``PATH`` the same way at invocation time), the
app checks for it once up front, exposes the result on the ``/health``
endpoint as a "degraded" warning, and -- mirroring
:mod:`collapsarr.jobs.failure_notify`'s bridge pattern for downmix failures
(COL-37) -- fans a :class:`~collapsarr.notify.dispatch.NotificationEvent` out
to every notifier enabled on the persisted
:class:`~collapsarr.notify.models.NotifierConfig` row, if any.

Deliberately never raises: :func:`notify_ffmpeg_missing` wraps everything in
a single ``try``/``except`` so a notification problem can never fail app
startup -- the same guarantee :func:`~collapsarr.jobs.failure_notify.
notify_job_failure` gives the downmix job it reports on.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from .notify import NotificationEvent, dispatch_notification, get_notifier_config

logger = logging.getLogger(__name__)

EVENT_TYPE = "health_check_failed"
DEFAULT_FFMPEG_PATH = "ffmpeg"


@dataclass(frozen=True, slots=True)
class FfmpegCheckResult:
    """Outcome of checking whether ``ffmpeg_path`` resolves on ``PATH``."""

    available: bool
    ffmpeg_path: str
    detail: str


def check_ffmpeg(ffmpeg_path: str = DEFAULT_FFMPEG_PATH) -> FfmpegCheckResult:
    """Check whether ``ffmpeg_path`` resolves to an executable on ``PATH``.

    A presence check only (:func:`shutil.which`) -- it does not invoke the
    binary or check its version, matching how
    :mod:`collapsarr.downmix.remux`/:mod:`collapsarr.downmix.probe` resolve
    ``ffmpeg``/``ffprobe`` themselves (a bare command name looked up on
    ``PATH`` at invocation time). Never raises.
    """
    resolved = shutil.which(ffmpeg_path)
    if resolved is None:
        return FfmpegCheckResult(
            available=False,
            ffmpeg_path=ffmpeg_path,
            detail=f"FFmpeg executable {ffmpeg_path!r} was not found on PATH.",
        )
    return FfmpegCheckResult(
        available=True,
        ffmpeg_path=ffmpeg_path,
        detail=f"FFmpeg found at {resolved!r}.",
    )


def _build_event(check: FfmpegCheckResult) -> NotificationEvent:
    """Build the :class:`NotificationEvent` describing a missing FFmpeg."""
    return NotificationEvent(
        event_type=EVENT_TYPE,
        title="FFmpeg not found",
        message=(
            "Collapsarr started but FFmpeg is missing -- downmix jobs will fail "
            "until it is installed and on PATH."
        ),
        details={"check": "ffmpeg", "ffmpeg_path": check.ffmpeg_path, "detail": check.detail},
    )


def notify_ffmpeg_missing(
    session: Session,
    check: FfmpegCheckResult,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Dispatch a health-check-failed notification for a missing FFmpeg.

    A no-op when ``check.available`` is ``True``. Otherwise reads the
    singleton :class:`~collapsarr.notify.models.NotifierConfig` row via
    ``session`` (creating it with defaults, i.e. both notifiers disabled, on
    first call) and fans the event out to every notifier enabled on it, same
    as :func:`~collapsarr.jobs.failure_notify.notify_job_failure`.
    ``transport`` is forwarded to :func:`~collapsarr.notify.dispatch.
    dispatch_notification` -- tests inject an ``httpx.MockTransport``;
    production leaves it ``None`` for a real network call.

    Never raises: any exception -- from reading the config, building the
    event, or dispatching it -- is caught and logged rather than propagated,
    so a notification problem can never fail app startup.
    """
    if check.available:
        return
    try:
        config = get_notifier_config(session)
        event = _build_event(check)
        dispatch_notification(config, event, transport=transport)
    except Exception:  # noqa: BLE001 - a notification problem must never fail startup
        logger.exception("Failed to dispatch FFmpeg-missing health notification")
