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
# Deliberate, PERMANENT exception to the `{SEVERITY}-{CATEGORY}-{SEQ}` Check Code
# glossary format (CONTEXT.md). This exact string is the code the `/health`
# liveness endpoint has surfaced since COL-38, and it is part of that endpoint's
# public response contract: the README documents it and the frontend health
# banner (frontend/src/test/healthBanner.test.tsx) matches on it. AC6 requires
# `/health` keep its exact current response shape, so this code must NOT be
# renamed to the glossary format -- doing so would break the public contract.
# Every *other* check (the 5 sibling COL-74 tickets) MUST use the glossary
# format; this one is the sole grandfathered exception, recorded in CONTEXT.md.
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
    fake to simulate a present/missing binary -- an injected fake's returned
    callable still ignores its context entirely (FFmpeg presence is
    process-global, not per-instance or DB-backed), matching the pre-COL-218
    behaviour exactly, so existing tests need no changes.

    With the real, un-overridden ``checker`` (identity-checked against
    :func:`check_ffmpeg`, the only way ``default_health_checks`` -- see
    :mod:`collapsarr.health.registry` -- ever resolves an un-overridden
    probe), the returned callable additionally reads ``context.session`` for
    the persisted :class:`~collapsarr.settings.models.GlobalSettings` row
    (COL-218) fresh on every tick, and -- only when its ``ffmpeg_path``
    override is set -- probes *that* path instead of the bare ``"ffmpeg"``
    default, so pointing Settings at a runtime-free native FFmpeg build
    (Epic COL-214) flips this check to "available" on the very next tick,
    no restart required. Unset (every fresh install's state, and every
    existing install's row after the additive migration) keeps probing the
    bare ``"ffmpeg"`` default -- byte-for-byte the pre-COL-218 behaviour.
    """

    def run(context: HealthCheckContext) -> Sequence[HealthCheckResult]:
        if checker is check_ffmpeg:
            from collapsarr.settings.service import get_global_settings

            configured_path = get_global_settings(context.session).ffmpeg_path
            if configured_path:
                return [_to_result(check_ffmpeg(configured_path))]
        return [_to_result(checker())]

    return run
