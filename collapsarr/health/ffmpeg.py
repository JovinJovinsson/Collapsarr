"""FFmpeg presence health check (COL-38 migrated into the COL-75 framework).

The original one-shot startup check (a ``shutil.which`` presence probe) and its
bespoke ``notify_ffmpeg_missing`` notifier are retired; the *probe itself* --
:func:`check_ffmpeg` and its :class:`FfmpegCheckResult` return type -- is kept
verbatim (same signature, same return type) and merely *adapted* into the
framework's shared :class:`~collapsarr.health.result.HealthCheckResult` shape by
:func:`make_ffmpeg_check_run`. Diffing that result against persisted state and
firing notifications on a transition is now the framework's job
(:mod:`collapsarr.health.service`), not this module's.

The failure Check Code stays ``"ffmpeg_missing"`` -- the exact code the
``/health`` endpoint has always surfaced -- so that endpoint's response is
byte-for-byte unchanged after the migration.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .context import HealthCheckContext
from .result import SEVERITY_ERROR, HealthCheckResult

FFMPEG_CHECK_NAME = "ffmpeg"
FFMPEG_MISSING_CODE = "ffmpeg_missing"
FFMPEG_CATEGORY = "ffmpeg"
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


def _to_result(check: FfmpegCheckResult) -> HealthCheckResult:
    """Adapt a :class:`FfmpegCheckResult` into the shared result shape."""
    return HealthCheckResult(
        code=FFMPEG_MISSING_CODE,
        category=FFMPEG_CATEGORY,
        severity=SEVERITY_ERROR,
        message=check.detail,
        passing=check.available,
    )


def make_ffmpeg_check_run(
    checker: Callable[[], FfmpegCheckResult] = check_ffmpeg,
) -> Callable[[HealthCheckContext], Sequence[HealthCheckResult]]:
    """Build the framework ``run`` callable wrapping the FFmpeg presence probe.

    ``checker`` defaults to the real :func:`check_ffmpeg`; the app/tests inject a
    fake to simulate a present/missing binary. The returned callable ignores its
    context (FFmpeg presence is process-global, not per-instance or DB-backed).
    """

    def run(_context: HealthCheckContext) -> Sequence[HealthCheckResult]:
        return [_to_result(checker())]

    return run
