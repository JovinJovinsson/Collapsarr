"""Validate a produced remux temp file and atomically swap it in (COL-18).

:func:`~collapsarr.downmix.remux.run_remux` (COL-17) writes a *temp* file —
in the same directory as the original, containing every original stream plus
the newly-encoded downmix track(s) — but deliberately stops short of touching
the original. This module is that final, safety-critical step: it validates
the temp file against the original and, **only** if it passes, atomically
renames it over the original.

This is the app's core data-safety guarantee. Collapsarr mutates users' real,
often-irreplaceable media libraries in place, so the contract here is
absolute: **on any outcome other than a fully-validated success, the original
file is left byte-for-byte untouched and the temp file is discarded.** No
long-term backup of the original is ever made — the operation is strictly
additive and the temp/rename dance *is* the safety window (see
``docs/plans/2026-07-20-collapsarr-v1-design.md``, "Remux safety").

Two checks gate the swap, both via :func:`~collapsarr.downmix.probe.probe_media_summary`
(real ffprobe, same injectable-runner seam as the rest of the engine):

1. **Duration** — the temp's container duration must match the original's
   within :data:`DEFAULT_DURATION_TOLERANCE_SECONDS`. Re-encoding one audio
   track and remuxing legitimately shifts the container duration by a few
   codec-frame boundaries (tens of milliseconds — an observed AC3 remux of a
   0.428s source came out 0.512s, ~84ms longer). The tolerance sits an order
   of magnitude above that yet far below any real truncation/corruption,
   which manifests as seconds-to-minutes. Erring *tight* is the safe
   direction: a false reject merely wastes the remux and leaves the original
   intact, whereas a false accept could swap in a corrupt file — so the
   tolerance is chosen to comfortably admit legitimate remuxes and nothing
   looser.
2. **Stream count** — the temp must have exactly ``original stream count +
   added_track_count`` streams. All streams are counted (video/audio/
   subtitle), so a remux that silently dropped or duplicated a stream is
   caught even though its duration would look fine.

The swap itself uses :meth:`Path.rename` (``rename(2)``), which on POSIX
atomically replaces the destination in a single syscall. Collapsarr's target
deployment is POSIX (Linux/macOS/Docker, the *arr ecosystem), and COL-17
guarantees the temp file lives in the original's own directory — hence on the
same filesystem — so the rename never degrades to a non-atomic cross-device
copy. There is therefore never a moment where the original is partially
written or missing.

Mirrors the testing pattern of :mod:`collapsarr.downmix.remux`: real committed
fixture media under ``tests/fixtures/downmix/`` driven through the actual
ffmpeg/ffprobe binaries for the success path and each real failure path
(a genuinely truncated temp for duration, a genuinely stream-dropped temp for
stream count), plus an injectable ``runner`` for fast, binary-free unit tests.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from collapsarr.downmix.probe import (
    MediaSummary,
    probe_media_summary,
)
from collapsarr.downmix.remux import RemuxResult

_DEFAULT_FFPROBE_PATH = "ffprobe"
_DEFAULT_TIMEOUT = 30.0

# See the module docstring for the full rationale. Sub-second, comfortably
# above the tens-of-ms shift a legitimate re-encode/remux introduces, far
# below any real truncation. Baked into the safety contract; override per-call
# only with good reason (and never loosen it lightly).
DEFAULT_DURATION_TOLERANCE_SECONDS = 0.5

_Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


class ApplyFailureReason(Enum):
    """Why an :func:`apply_remux_result` validation refused to swap."""

    DURATION_MISMATCH = "duration_mismatch"
    STREAM_COUNT_MISMATCH = "stream_count_mismatch"


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of an :func:`apply_remux_result` attempt, shaped for job history.

    On success ``applied_path`` is the original path (now holding the remuxed
    file) and ``failure_reason`` is ``None``. On a validation failure
    ``applied_path`` is ``None``, ``failure_reason`` names the specific check
    that failed, and — guaranteed — the original file is unchanged and the
    temp file has been deleted. ``detail`` is always a human-readable summary
    (the concrete durations/counts involved) suitable for logging.
    """

    success: bool
    applied_path: Path | None
    failure_reason: ApplyFailureReason | None
    detail: str


def apply_remux_result(
    original_path: str | Path,
    remux_result: RemuxResult,
    added_track_count: int,
    *,
    duration_tolerance_seconds: float = DEFAULT_DURATION_TOLERANCE_SECONDS,
    ffprobe_path: str = _DEFAULT_FFPROBE_PATH,
    timeout: float = _DEFAULT_TIMEOUT,
    runner: _Runner | None = None,
) -> ApplyResult:
    """Validate ``remux_result``'s temp file and atomically swap it over the original.

    Re-probes both the (still-untouched) original and the temp file, then:

    - if the temp's container duration is within
      ``duration_tolerance_seconds`` of the original's **and** its total
      stream count equals the original's plus ``added_track_count``, renames
      the temp over the original (atomic on POSIX / same filesystem) and
      returns ``ApplyResult(success=True, ...)``;
    - otherwise deletes the temp file, leaves the original byte-for-byte
      untouched, and returns ``ApplyResult(success=False, ...)`` naming the
      failing check.

    The original is never modified on any path except the successful atomic
    rename, and no backup copy of it is ever created — the temp file is the
    only extra artifact, and it is gone by the time this returns.

    ``added_track_count`` is the number of new downmix tracks the remux was
    asked to add (i.e. ``len(qualifying_targets)``); the original's own stream
    count is measured fresh here rather than trusted from the caller.

    Raises:
        ValueError: ``remux_result`` did not succeed (so there is no temp
            file to apply), or ``added_track_count`` is negative — both are
            caller/programmer errors, mirroring
            :func:`~collapsarr.downmix.remux.run_remux`'s use of ``ValueError``
            for bad inputs.
        collapsarr.downmix.probe.FfprobeError: either file could not be
            probed (missing binary, timeout, non-zero exit, unparseable/
            incomplete output). The temp file is deleted and the original left
            untouched before the error propagates, so no orphan is left and no
            unvalidated swap ever happens.
    """
    if not remux_result.success or remux_result.temp_file_path is None:
        raise ValueError(
            "apply_remux_result requires a successful RemuxResult with a temp file"
        )
    if added_track_count < 0:
        raise ValueError(f"added_track_count must not be negative, got {added_track_count}")

    original = Path(original_path)
    temp = remux_result.temp_file_path

    # Any inability to *validate* must never green-light a swap. Probe errors
    # therefore clean up the temp (no orphan, matching run_remux's contract)
    # and propagate, leaving the original untouched.
    try:
        original_summary = probe_media_summary(
            original, ffprobe_path=ffprobe_path, timeout=timeout, runner=runner
        )
        temp_summary = probe_media_summary(
            temp, ffprobe_path=ffprobe_path, timeout=timeout, runner=runner
        )
    except BaseException:
        _remove_if_exists(temp)
        raise

    duration_delta = abs(temp_summary.duration_seconds - original_summary.duration_seconds)
    if duration_delta > duration_tolerance_seconds:
        _remove_if_exists(temp)
        return ApplyResult(
            success=False,
            applied_path=None,
            failure_reason=ApplyFailureReason.DURATION_MISMATCH,
            detail=_duration_mismatch_detail(
                original_summary, temp_summary, duration_delta, duration_tolerance_seconds
            ),
        )

    expected_stream_count = original_summary.stream_count + added_track_count
    if temp_summary.stream_count != expected_stream_count:
        _remove_if_exists(temp)
        return ApplyResult(
            success=False,
            applied_path=None,
            failure_reason=ApplyFailureReason.STREAM_COUNT_MISMATCH,
            detail=_stream_count_mismatch_detail(
                original_summary, temp_summary, added_track_count, expected_stream_count
            ),
        )

    # Both checks passed: atomically replace the original. On POSIX / same
    # filesystem this is a single rename(2) — no partial-write window.
    temp.rename(original)
    return ApplyResult(
        success=True,
        applied_path=original,
        failure_reason=None,
        detail=(
            f"applied remux over {str(original)!r}: "
            f"{original_summary.stream_count}->{temp_summary.stream_count} streams, "
            f"duration {original_summary.duration_seconds:.3f}s->"
            f"{temp_summary.duration_seconds:.3f}s "
            f"(delta {duration_delta:.3f}s within {duration_tolerance_seconds:.3f}s)"
        ),
    )


def _duration_mismatch_detail(
    original: MediaSummary,
    temp: MediaSummary,
    delta: float,
    tolerance: float,
) -> str:
    return (
        f"duration mismatch: original {original.duration_seconds:.3f}s vs "
        f"remux {temp.duration_seconds:.3f}s (delta {delta:.3f}s exceeds "
        f"tolerance {tolerance:.3f}s); original left untouched, temp discarded"
    )


def _stream_count_mismatch_detail(
    original: MediaSummary,
    temp: MediaSummary,
    added_track_count: int,
    expected: int,
) -> str:
    return (
        f"stream-count mismatch: remux has {temp.stream_count} streams, expected "
        f"{expected} (original {original.stream_count} + {added_track_count} added); "
        f"original left untouched, temp discarded"
    )


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
