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

A third, optional check runs **after** the swap (COL-241): when the caller
passes ``expected_default_audio_index`` (the no-Plex-Connection remux
fallback for the Default Audio Track Job — :mod:`collapsarr.downmix.
default_audio_pipeline` — is currently the only caller that does), the
now-swapped-in file at ``original_path`` is re-probed via
:func:`~collapsarr.downmix.probe.probe_audio_streams` and the resolved
target output audio index is confirmed to be the *only* audio stream
carrying ``disposition.default``. Unlike the two pre-swap checks, neither a
disposition mismatch nor a failure of the re-probe itself can be healed by
discarding the temp file — the swap has already happened and no backup of
the original is ever kept (see above) — so this check cannot preserve the
"original left untouched" guarantee the way the duration/stream-count
checks do. It exists purely so a disposition bug is reported as a distinct,
loud Job failure instead of silently reporting success, which is the gap
COL-241 closes — and the two ways it can fail are themselves kept distinct:
a completed-but-wrong re-probe is :attr:`ApplyFailureReason.
DISPOSITION_MISMATCH`; the re-probe itself erroring (so verification never
actually completed) is :attr:`ApplyFailureReason.
DISPOSITION_VERIFICATION_FAILED`. Collapsing those two into one generic
"couldn't validate" error would silently re-introduce the exact ambiguity
this ticket exists to close: an operator couldn't tell "the swap happened
and disposition is confirmed wrong" from "the swap happened and disposition
was never actually checked".

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
    AudioStreamInfo,
    FfprobeError,
    MediaSummary,
    probe_audio_streams,
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
    DISPOSITION_MISMATCH = "disposition_mismatch"
    DISPOSITION_VERIFICATION_FAILED = "disposition_verification_failed"


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of an :func:`apply_remux_result` attempt, shaped for job history.

    On success ``applied_path`` is the original path (now holding the remuxed
    file) and ``failure_reason`` is ``None``. On a validation failure
    ``applied_path`` is ``None``, ``failure_reason`` names the specific check
    that failed, and — guaranteed for :attr:`ApplyFailureReason.
    DURATION_MISMATCH`/:attr:`ApplyFailureReason.STREAM_COUNT_MISMATCH` — the
    original file is unchanged and the temp file has been deleted.
    :attr:`ApplyFailureReason.DISPOSITION_MISMATCH` and :attr:`ApplyFailureReason.
    DISPOSITION_VERIFICATION_FAILED` are the two exceptions: both are only
    ever produced *after* the atomic swap already happened (see the module
    docstring), so on either failure the swapped-in file is what's left on
    disk at ``original_path`` — not reverted, since no backup of the
    pre-swap original is ever kept. The two are distinct so a caller/operator
    can tell them apart: ``DISPOSITION_MISMATCH`` means verification *ran*
    and found the wrong (or no) stream flagged default; ``DISPOSITION_
    VERIFICATION_FAILED`` means verification itself could not complete (the
    post-swap re-probe errored) — the swap happened but whether the
    disposition landed correctly was never actually confirmed either way.
    ``detail`` is always a human-readable summary (the concrete
    durations/counts/streams involved) suitable for logging.
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
    expected_default_audio_index: int | None = None,
) -> ApplyResult:
    """Validate ``remux_result``'s temp file and atomically swap it over the original.

    Re-probes both the (still-untouched) original and the temp file, then:

    - if the temp's container duration is within
      ``duration_tolerance_seconds`` of the original's **and** its total
      stream count equals the original's plus ``added_track_count``, renames
      the temp over the original (atomic on POSIX / same filesystem);
    - otherwise deletes the temp file, leaves the original byte-for-byte
      untouched, and returns ``ApplyResult(success=False, ...)`` naming the
      failing check.

    The original is never modified on any path except the successful atomic
    rename, and no backup copy of it is ever created — the temp file is the
    only extra artifact, and it is gone by the time this returns.

    ``added_track_count`` is the number of new downmix tracks the remux was
    asked to add (i.e. ``len(qualifying_targets)``); the original's own stream
    count is measured fresh here rather than trusted from the caller.

    ``expected_default_audio_index`` (COL-241) is the output audio-relative
    index (same numbering as :func:`~collapsarr.downmix.remux.
    build_remux_command`'s ``-disposition:a:N`` flags) the remux was asked to
    make the Default Audio Track. When supplied, **after** the atomic rename
    this re-probes the swapped-in file's audio streams via
    :func:`~collapsarr.downmix.probe.probe_audio_streams` and confirms that
    stream — and no other audio stream — carries ``disposition.default``,
    returning one of two distinct failures instead of reporting success when
    it can't confirm that:

    - the re-probe **completes** but finds the wrong stream (or no stream, or
      more than one stream) flagged default: ``ApplyResult(success=False,
      failure_reason=ApplyFailureReason.DISPOSITION_MISMATCH, ...)``;
    - the re-probe **itself fails** to complete (missing binary, timeout,
      non-zero exit, unparseable output) — so verification was never actually
      performed at all: ``ApplyResult(success=False, failure_reason=
      ApplyFailureReason.DISPOSITION_VERIFICATION_FAILED, ...)``, distinct
      from the first so a caller/operator can't mistake "confirmed wrong" for
      "never confirmed".

    Either way, unlike the duration/stream-count checks, neither can undo the
    swap that already happened (see the module docstring). Leaving this
    ``None`` (the default) skips the check entirely, preserving prior
    behaviour byte-for-byte for every caller that doesn't pass it.

    Raises:
        ValueError: ``remux_result`` did not succeed (so there is no temp
            file to apply), or ``added_track_count`` is negative — both are
            caller/programmer errors, mirroring
            :func:`~collapsarr.downmix.remux.run_remux`'s use of ``ValueError``
            for bad inputs.
        collapsarr.downmix.probe.FfprobeError: the *pre-swap* probe of the
            original or the temp file could not be completed (missing
            binary, timeout, non-zero exit, unparseable/incomplete output).
            The temp file is deleted and the original left untouched before
            the error propagates, so no orphan is left and no unvalidated
            swap ever happens. The *post-swap* re-probe (when
            ``expected_default_audio_index`` is supplied) never raises this
            — it has no cleanup left to do, the swap already succeeded — its
            failure is instead reported as ``ApplyResult(failure_reason=
            ApplyFailureReason.DISPOSITION_VERIFICATION_FAILED, ...)`` above,
            precisely so it can't be conflated with (or silently swallowed
            alongside) a pre-swap probe failure by a caller's blanket
            ``except FfprobeError``.
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

    disposition_note = ""
    if expected_default_audio_index is not None:
        # Unlike the two checks above, there is no temp file left to discard
        # and no original left to protect -- the swap already happened. A
        # mismatch here is reported as a distinct Job failure (COL-241), not
        # silently treated as success, but it cannot be reverted.
        #
        # The re-probe's own failure is caught here -- deliberately *not*
        # left to propagate as FfprobeError like the pre-swap probes above.
        # A caller with one blanket `except FfprobeError` around the whole
        # apply_remux_result() call (e.g. run_default_audio_pipeline) would
        # otherwise report this identically to a pre-swap probe failure, even
        # though the safety implications are opposite: pre-swap, the original
        # is untouched; here, the swap already happened and disposition was
        # never actually confirmed either way. Reporting it as a distinct
        # ApplyResult keeps that "verification never completed" case
        # separate from both "confirmed wrong" (DISPOSITION_MISMATCH) and any
        # pre-swap failure.
        try:
            post_swap_streams = probe_audio_streams(
                original, ffprobe_path=ffprobe_path, timeout=timeout, runner=runner
            )
        except FfprobeError as exc:
            return ApplyResult(
                success=False,
                applied_path=None,
                failure_reason=ApplyFailureReason.DISPOSITION_VERIFICATION_FAILED,
                detail=(
                    "disposition verification failed: could not re-probe "
                    f"{str(original)!r} after the atomic swap ({exc}); swap "
                    "already applied, disposition never confirmed either way"
                ),
            )
        mismatch_detail = _disposition_mismatch_detail(
            post_swap_streams, expected_default_audio_index
        )
        if mismatch_detail is not None:
            return ApplyResult(
                success=False,
                applied_path=None,
                failure_reason=ApplyFailureReason.DISPOSITION_MISMATCH,
                detail=mismatch_detail,
            )
        disposition_note = (
            f"; disposition verified on output audio stream a:{expected_default_audio_index}"
        )

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
            f"{disposition_note}"
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


def _disposition_mismatch_detail(
    post_swap_streams: Sequence[AudioStreamInfo],
    expected_default_audio_index: int,
) -> str | None:
    """Check the post-swap audio streams against the expected disposition winner.

    Returns ``None`` when ``post_swap_streams[expected_default_audio_index]``
    is the *sole* audio stream carrying ``disposition.default`` (a pass), or a
    human-readable mismatch detail otherwise (a fail): the expected index is
    out of range, that stream isn't flagged default, or some other stream is
    wrongly flagged default too (either instead of, or in addition to, the
    expected one).

    The "sole default index" check this makes is conceptually the same one
    :func:`~collapsarr.downmix.default_audio.resolve_default_audio_output_index`
    and :mod:`collapsarr.downmix.default_audio_pipeline`'s ``_already_correct``
    each already make over a *pre*-swap stream list -- kept as its own small,
    module-local check here (over the *post*-swap list) rather than importing
    either, matching this codebase's existing precedent of duplicating tiny,
    single-purpose predicates across modules instead of adding a cross-module
    dependency for a few lines (see e.g. ``_TARGET_CHANNELS`` in
    :mod:`collapsarr.downmix.remux`/:mod:`collapsarr.downmix.default_audio`).
    """
    defaulted = [i for i, stream in enumerate(post_swap_streams) if stream.is_default]

    if not 0 <= expected_default_audio_index < len(post_swap_streams):
        return (
            f"disposition mismatch: expected output audio index "
            f"{expected_default_audio_index} is out of range for "
            f"{len(post_swap_streams)} post-swap audio stream(s) "
            f"(streams flagged default: {defaulted}); swap already applied, not reverted"
        )

    if defaulted != [expected_default_audio_index]:
        return (
            f"disposition mismatch: expected only output audio stream "
            f"a:{expected_default_audio_index} to carry disposition.default, but "
            f"{defaulted} do; swap already applied, not reverted"
        )

    return None


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
