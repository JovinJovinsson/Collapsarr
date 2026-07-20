"""End-to-end single-file downmix pipeline (COL-19).

The demoable tracer bullet for the Downmix Engine: wires the four prior
modules — :mod:`~collapsarr.downmix.probe` (COL-15),
:mod:`~collapsarr.downmix.targets` (COL-16),
:mod:`~collapsarr.downmix.remux` (COL-17), and
:mod:`~collapsarr.downmix.apply` (COL-18) — into one callable pipeline for a
single file: probe -> detect qualifying targets -> (no-op if none) -> remux
-> validate/apply.

Enabled targets, the language allow-list, and codec/bitrate overrides come
from a caller-supplied :class:`~collapsarr.downmix.targets.DownmixSettings`.
There is no persisted Settings model yet (that's future work); this pipeline
simply takes one as an argument, the same way :func:`detect_qualifying_targets`
and :func:`run_remux` already do.

:func:`run_downmix_pipeline` never raises for a *runtime* failure at any
stage (ffprobe/ffmpeg missing, non-zero exit, timeout, or a failed
validate-and-apply) — every outcome, success or failure, comes back as one
:class:`PipelineResult` shaped for job history: which stage produced it
(:class:`PipelineOutcome`), the tracks added on success, and — on failure —
the underlying :class:`~collapsarr.downmix.remux.RemuxResult` or
:class:`~collapsarr.downmix.apply.ApplyResult` (whichever stage failed),
reusing their exit-code/stderr/failure-reason fields rather than
re-inventing them. A file with no qualifying targets is reported as a
distinct, non-error outcome (:attr:`PipelineOutcome.NOTHING_TO_DO`), never
as a failure.

Not yet wired into a job queue — that's COL-20's concern. This module is the
callable, single-file seam a job runner will later invoke per file.

Tests mirror the rest of the engine: real committed fixture media under
``tests/fixtures/downmix/`` driven through the actual ffmpeg/ffprobe binaries
end-to-end for the success, nothing-to-do, and real-ffmpeg-failure paths
(skipped when ffmpeg/ffprobe aren't installed), plus an injectable ``runner``
for fast, environment-independent unit tests of the stage-by-stage control
flow.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from collapsarr.downmix.apply import (
    DEFAULT_DURATION_TOLERANCE_SECONDS,
    ApplyResult,
    apply_remux_result,
)
from collapsarr.downmix.probe import FfprobeError, probe_audio_streams
from collapsarr.downmix.remux import RemuxResult, run_remux
from collapsarr.downmix.targets import DownmixSettings, QualifyingTarget, detect_qualifying_targets

_DEFAULT_FFPROBE_PATH = "ffprobe"
_DEFAULT_FFMPEG_PATH = "ffmpeg"
_DEFAULT_PROBE_TIMEOUT = 30.0
_DEFAULT_REMUX_TIMEOUT = 3600.0

_Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


class PipelineOutcome(Enum):
    """Which stage of the pipeline a :class:`PipelineResult` came from."""

    SUCCESS = "success"
    NOTHING_TO_DO = "nothing_to_do"
    PROBE_FAILED = "probe_failed"
    REMUX_FAILED = "remux_failed"
    APPLY_FAILED = "apply_failed"


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Structured outcome of one end-to-end :func:`run_downmix_pipeline` run.

    Shaped for job history. ``success`` is ``True`` only for
    :attr:`PipelineOutcome.SUCCESS` — a no-op (nothing to do) is reported
    separately via ``outcome`` rather than folded into either success or
    failure, since it is neither: nothing was attempted, nothing failed.

    ``tracks_added`` is populated (non-empty) only on success — the
    ``(language, target)`` pairs that were added.

    ``remux_result``/``apply_result`` carry the raw result from whichever
    stage ran, so a caller gets the ffmpeg exit code/stderr
    (:class:`~collapsarr.downmix.remux.RemuxResult`) or the validation
    failure reason/detail
    (:class:`~collapsarr.downmix.apply.ApplyResult`) straight from the
    source rather than this module re-deriving them. Both are ``None`` when
    the pipeline never reached that stage (e.g. a probe failure, or nothing
    to do).

    ``detail`` is always a human-readable one-line summary suitable for
    logging, regardless of outcome.
    """

    outcome: PipelineOutcome
    success: bool
    detail: str
    tracks_added: tuple[QualifyingTarget, ...] = ()
    remux_result: RemuxResult | None = None
    apply_result: ApplyResult | None = None


def run_downmix_pipeline(
    file_path: str | Path,
    settings: DownmixSettings,
    *,
    ffprobe_path: str = _DEFAULT_FFPROBE_PATH,
    ffmpeg_path: str = _DEFAULT_FFMPEG_PATH,
    probe_timeout: float = _DEFAULT_PROBE_TIMEOUT,
    remux_timeout: float = _DEFAULT_REMUX_TIMEOUT,
    duration_tolerance_seconds: float = DEFAULT_DURATION_TOLERANCE_SECONDS,
    runner: _Runner | None = None,
) -> PipelineResult:
    """Run the full downmix pipeline for a single file, end to end.

    In order: :func:`~collapsarr.downmix.probe.probe_audio_streams`,
    :func:`~collapsarr.downmix.targets.detect_qualifying_targets`,
    :func:`~collapsarr.downmix.remux.run_remux`, and
    :func:`~collapsarr.downmix.apply.apply_remux_result` — stopping early
    (and reporting why) as soon as a stage has nothing left to hand the
    next one.

    ``runner`` overrides how every subprocess call across all three stages
    is invoked (signature ``(command, timeout) -> subprocess.CompletedProcess``,
    the same seam :func:`~collapsarr.downmix.probe.probe_audio_streams`,
    :func:`~collapsarr.downmix.remux.run_remux`, and
    :func:`~collapsarr.downmix.apply.apply_remux_result` each already
    accept); it defaults to real subprocesses and exists so tests can drive
    the whole pipeline's control flow without the ``ffmpeg``/``ffprobe``
    binaries installed.

    Never raises for a runtime failure at any stage:

    - a probing failure (missing binary, non-zero exit, unparseable output)
      is reported as :attr:`PipelineOutcome.PROBE_FAILED`;
    - no qualifying targets is reported as
      :attr:`PipelineOutcome.NOTHING_TO_DO` (``success=True`` — this is not
      an error);
    - an ffmpeg remux failure is reported as
      :attr:`PipelineOutcome.REMUX_FAILED`, carrying the
      :class:`~collapsarr.downmix.remux.RemuxResult` (exit code + stderr);
      the original file is untouched (:func:`run_remux`'s own guarantee);
    - a failed validate-and-apply (duration/stream-count mismatch, or the
      temp/original becoming unprobeable after the remux) is reported as
      :attr:`PipelineOutcome.APPLY_FAILED`, carrying the
      :class:`~collapsarr.downmix.apply.ApplyResult` when one was produced;
      the original file is untouched
      (:func:`~collapsarr.downmix.apply.apply_remux_result`'s own
      guarantee).

    On success, the original file has been atomically replaced by the
    remuxed version (all original streams intact, plus the newly added
    downmix track(s)), and ``tracks_added`` lists what was added.
    """
    path = Path(file_path)

    try:
        streams = probe_audio_streams(
            path, ffprobe_path=ffprobe_path, timeout=probe_timeout, runner=runner
        )
    except FfprobeError as exc:
        return PipelineResult(
            outcome=PipelineOutcome.PROBE_FAILED,
            success=False,
            detail=f"failed to probe audio streams of {str(path)!r}: {exc}",
        )

    targets = detect_qualifying_targets(streams, settings)
    if not targets:
        return PipelineResult(
            outcome=PipelineOutcome.NOTHING_TO_DO,
            success=True,
            detail=f"no qualifying downmix targets for {str(path)!r}; nothing to do",
        )

    remux_result = run_remux(
        path,
        streams,
        targets,
        settings,
        ffmpeg_path=ffmpeg_path,
        timeout=remux_timeout,
        runner=runner,
    )
    if not remux_result.success:
        return PipelineResult(
            outcome=PipelineOutcome.REMUX_FAILED,
            success=False,
            detail=(
                f"ffmpeg remux failed for {str(path)!r} "
                f"(exit code {remux_result.returncode}): {remux_result.stderr.strip()}"
            ),
            remux_result=remux_result,
        )

    try:
        apply_result = apply_remux_result(
            path,
            remux_result,
            added_track_count=len(targets),
            duration_tolerance_seconds=duration_tolerance_seconds,
            ffprobe_path=ffprobe_path,
            timeout=probe_timeout,
            runner=runner,
        )
    except FfprobeError as exc:
        return PipelineResult(
            outcome=PipelineOutcome.APPLY_FAILED,
            success=False,
            detail=f"failed to validate remux result for {str(path)!r}: {exc}",
            remux_result=remux_result,
        )

    if not apply_result.success:
        return PipelineResult(
            outcome=PipelineOutcome.APPLY_FAILED,
            success=False,
            detail=apply_result.detail,
            remux_result=remux_result,
            apply_result=apply_result,
        )

    return PipelineResult(
        outcome=PipelineOutcome.SUCCESS,
        success=True,
        detail=apply_result.detail,
        tracks_added=tuple(targets),
        remux_result=remux_result,
        apply_result=apply_result,
    )
