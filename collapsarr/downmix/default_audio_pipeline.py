"""Disposition-only pipeline for manual/bulk Default Audio Track fixes (COL-153).

:mod:`~collapsarr.downmix.pipeline` (COL-19)'s
:func:`~collapsarr.downmix.pipeline.run_downmix_pipeline` is the end-to-end
downmix pipeline; COL-152 folded an *automatic*, opt-in Default Audio Track
fix into that same pipeline, piggybacking on an outbound downmix remux. This
module is the sibling pipeline for the other half of the Preferred Default
Audio feature: a *manual/bulk* fix a user (or a future bulk job, COL-155)
asks for directly, against files that may not qualify for any downmix at
all.

:func:`run_default_audio_pipeline` probes a file, resolves which of its
*existing* audio streams should carry the Default Audio Track disposition
via :func:`~collapsarr.downmix.default_audio.resolve_default_audio_stream`
— deliberately that function, not
:func:`~collapsarr.downmix.default_audio.resolve_default_audio_output_index`,
which resolves over a downmix job's *prospective* final layout (existing
streams plus tracks it is about to encode). This pipeline never encodes
anything, so there is no "final layout" other than what already exists —
and, only if the winner differs from what the file already has, performs a
disposition-only remux through the *exact same* remux/atomic-swap safety
machinery :func:`run_downmix_pipeline` uses:
:func:`~collapsarr.downmix.remux.run_remux` (called with an always-empty
``qualifying_targets`` — see its docstring for the guard that permits this
specifically when a ``default_audio_index`` is supplied — so the ffmpeg
command is a plain ``-map 0`` / ``-c copy`` stream-copy of every original
stream, no new encoded audio track, ever) followed by
:func:`~collapsarr.downmix.apply.apply_remux_result`'s duration/stream-count
validation and atomic swap (``added_track_count=0``, since nothing new was
added), which is passed the resolved winner index as
``expected_default_audio_index`` (COL-241) so it re-probes the swapped-in
file afterward and confirms that stream — and no other audio stream — really
does carry ``disposition.default`` before this reports success. If the
winner already matches — it already carries the disposition, and no other
stream wrongly carries it too — this returns a no-op success without
invoking ffmpeg or touching the file at all, mirroring
:attr:`~collapsarr.downmix.pipeline.PipelineOutcome.NOTHING_TO_DO`'s "nothing
was attempted, nothing failed" contract.

Reuses :class:`~collapsarr.downmix.pipeline.PipelineResult` /
:class:`~collapsarr.downmix.pipeline.PipelineOutcome` rather than inventing
parallel types — the shape (which stage produced the outcome, the raw
``RemuxResult``/``ApplyResult`` for job-history storage) is identical;
``tracks_added`` is simply always empty here, since this pipeline never adds
a track.

Unlike COL-152's automatic fix, this pipeline is not gated by the
``auto_set_default_audio`` toggle — a manual/bulk request is, by definition,
an explicit ask, so it always attempts the fix when invoked. Wiring this
into a job kind, the queue/scheduler, and a trigger endpoint is COL-155's
concern; this module is the callable, single-file seam a job runner (or an
API handler) will later invoke per file, mirroring how
:func:`run_downmix_pipeline` was COL-19's seam before COL-20 wired it into
the job queue.

Logs lifecycle events the same way :func:`run_downmix_pipeline` does
(COL-129): the one non-fatal skip (:attr:`PipelineOutcome.NOTHING_TO_DO`)
at ``WARNING``, every genuine failure at ``ERROR`` (with
:attr:`PipelineOutcome.REMUX_FAILED` additionally logging a truncated
ffmpeg-stderr tail), a ``SUCCESS`` not logged here for the same reason —
job-level context belongs to the caller.

Tests mirror :mod:`tests.test_downmix_pipeline`: real committed fixture
media under ``tests/fixtures/downmix/`` driven through the actual
ffmpeg/ffprobe binaries for the changed, no-op, and validation-failure
cases (skipped when ffmpeg/ffprobe aren't installed), plus an injectable
``runner`` for fast, environment-independent unit tests of the
stage-by-stage control flow.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from collapsarr.downmix.apply import DEFAULT_DURATION_TOLERANCE_SECONDS, apply_remux_result
from collapsarr.downmix.cancellation import CancellationHandle, make_cancellable_runner
from collapsarr.downmix.default_audio import DefaultAudioPreference, resolve_default_audio_stream
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.downmix.probe import AudioStreamInfo, FfprobeError, probe_audio_streams
from collapsarr.downmix.remux import run_remux
from collapsarr.downmix.targets import DownmixSettings

logger = logging.getLogger(__name__)

_DEFAULT_FFPROBE_PATH = "ffprobe"
_DEFAULT_FFMPEG_PATH = "ffmpeg"
_DEFAULT_PROBE_TIMEOUT = 30.0
_DEFAULT_REMUX_TIMEOUT = 3600.0

#: Same cap, same rationale, as :mod:`collapsarr.downmix.pipeline` (COL-129) —
#: a private, module-local copy rather than importing that module's private
#: constant, matching how :mod:`collapsarr.downmix.default_audio` already
#: keeps its own copy of :mod:`collapsarr.downmix.remux`'s channel-count
#: mapping for the same reason (a fixed, tiny constant, not worth a
#: shared-module dependency to avoid a few duplicated lines).
_STDERR_LOG_CHARS = 2000

# No new tracks are ever encoded by this pipeline (see module docstring) --
# `run_remux` needs *some* `DownmixSettings` for its signature, but with
# `qualifying_targets` always empty here none of its codec/bitrate fields are
# ever read. A bare default instance stands in.
_UNUSED_DOWNMIX_SETTINGS = DownmixSettings()

_Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def _tail_for_log(text: str, limit: int = _STDERR_LOG_CHARS) -> str:
    """Return the last ``limit`` characters of ``text``, for log output only."""
    if len(text) <= limit:
        return text
    return text[-limit:]


def _finish(result: PipelineResult, level: int) -> PipelineResult:
    """Log ``result.detail`` at ``level`` and return ``result`` (COL-129)."""
    logger.log(level, result.detail)
    return result


def run_default_audio_pipeline(
    file_path: str | Path,
    preference: DefaultAudioPreference,
    *,
    ffprobe_path: str = _DEFAULT_FFPROBE_PATH,
    ffmpeg_path: str = _DEFAULT_FFMPEG_PATH,
    probe_timeout: float = _DEFAULT_PROBE_TIMEOUT,
    remux_timeout: float = _DEFAULT_REMUX_TIMEOUT,
    duration_tolerance_seconds: float = DEFAULT_DURATION_TOLERANCE_SECONDS,
    runner: _Runner | None = None,
    cancel_handle: CancellationHandle | None = None,
    expected_stream_count: int | None = None,
) -> PipelineResult:
    """Fix a single file's Default Audio Track disposition, on demand, no downmixing.

    ``expected_stream_count`` (COL-251) is accepted for call-signature parity
    with :func:`~collapsarr.plex.default_audio_write.apply_default_audio_via_plex`
    -- :class:`~collapsarr.jobs.queue.JobQueue` forwards a ``SET_DEFAULT_AUDIO``
    job's ``expected_stream_count`` to whichever mechanism the COL-247
    mechanism-selection gate routes to, and this pipeline is one of the two.
    It is unused here: this pipeline always re-probes the *actual* local
    file, which already reflects the downmix's real output the instant the
    remux completed -- there is no Plex-ingestion race to guard against on
    this (no-Plex) path, unlike the direct Plex API write.

    In order: :func:`~collapsarr.downmix.probe.probe_audio_streams`,
    :func:`~collapsarr.downmix.default_audio.resolve_default_audio_stream`,
    (no-op if the winner already matches) :func:`~collapsarr.downmix.remux.run_remux`
    with an empty ``qualifying_targets``, and
    :func:`~collapsarr.downmix.apply.apply_remux_result` with
    ``added_track_count=0`` — stopping early (and reporting why) exactly like
    :func:`~collapsarr.downmix.pipeline.run_downmix_pipeline`.

    ``runner`` overrides how every subprocess call is invoked (signature
    ``(command, timeout) -> subprocess.CompletedProcess``), the same seam
    :func:`~collapsarr.downmix.pipeline.run_downmix_pipeline` accepts; it
    defaults to real subprocesses.

    Never raises for a runtime failure at any stage — mirroring
    :func:`~collapsarr.downmix.pipeline.run_downmix_pipeline`:

    - a probing failure is reported as
      :attr:`~collapsarr.downmix.pipeline.PipelineOutcome.PROBE_FAILED`;
    - fewer than two audio streams to compare, or a resolved winner that
      already carries the disposition (and no other stream wrongly carries
      it too), is reported as
      :attr:`~collapsarr.downmix.pipeline.PipelineOutcome.NOTHING_TO_DO`
      (``success=True``) — no temp file is created, no swap performed, the
      file is left byte-for-byte untouched;
    - an ffmpeg remux failure is reported as
      :attr:`~collapsarr.downmix.pipeline.PipelineOutcome.REMUX_FAILED`; the
      original file is untouched (:func:`~collapsarr.downmix.remux.run_remux`'s
      own guarantee);
    - a failed validate-and-apply (duration/stream-count mismatch, or the
      *pre-swap* probe of the original/temp becoming unprobeable) is reported
      as :attr:`~collapsarr.downmix.pipeline.PipelineOutcome.APPLY_FAILED`;
      the original file is untouched
      (:func:`~collapsarr.downmix.apply.apply_remux_result`'s own guarantee).
      A post-swap disposition mismatch, or the post-swap re-probe itself
      failing (COL-241 — :attr:`~collapsarr.downmix.apply.ApplyFailureReason.
      DISPOSITION_MISMATCH`/:attr:`~collapsarr.downmix.apply.
      ApplyFailureReason.DISPOSITION_VERIFICATION_FAILED` respectively), is
      also reported as
      :attr:`~collapsarr.downmix.pipeline.PipelineOutcome.APPLY_FAILED`, but
      is the one case where the file is *not* left untouched — the swap
      already happened by the time either check runs, and there is no backup
      to revert to (see :func:`~collapsarr.downmix.apply.apply_remux_result`,
      whose ``ApplyResult.failure_reason`` keeps the two distinguishable
      rather than both surfacing as this function's own ``FfprobeError``
      handling below, which only ever covers the *pre*-swap probes).

    On success, the original file has been atomically replaced by a remux
    identical to it except for which stream(s) carry the Default Audio Track
    disposition flag — every stream stream-copied, none re-encoded,
    ``tracks_added`` always empty (this pipeline never adds a track).

    ``cancel_handle`` (COL-192) behaves exactly as in
    :func:`~collapsarr.downmix.pipeline.run_downmix_pipeline`: when supplied
    (and no explicit ``runner`` is given) every subprocess runs through
    :func:`~collapsarr.downmix.cancellation.make_cancellable_runner`, so a
    ``RUNNING`` ``SET_DEFAULT_AUDIO`` job is hard-killable the same way a
    ``DOWNMIX`` one is.
    """
    path = Path(file_path)

    if runner is None and cancel_handle is not None:
        runner = make_cancellable_runner(cancel_handle)

    try:
        streams = probe_audio_streams(
            path, ffprobe_path=ffprobe_path, timeout=probe_timeout, runner=runner
        )
    except FfprobeError as exc:
        return _finish(
            PipelineResult(
                outcome=PipelineOutcome.PROBE_FAILED,
                success=False,
                detail=f"failed to probe audio streams of {str(path)!r}: {exc}",
            ),
            logging.ERROR,
        )

    winner = resolve_default_audio_stream(streams, preference)
    if winner is None:
        return _finish(
            PipelineResult(
                outcome=PipelineOutcome.NOTHING_TO_DO,
                success=True,
                detail=(
                    f"fewer than two audio streams to compare in {str(path)!r}; "
                    "nothing to do"
                ),
            ),
            logging.WARNING,
        )

    winner_index = _index_of(streams, winner)
    if _already_correct(streams, winner_index):
        return _finish(
            PipelineResult(
                outcome=PipelineOutcome.NOTHING_TO_DO,
                success=True,
                detail=(
                    f"Default Audio Track disposition in {str(path)!r} already "
                    "matches the preference; nothing to do"
                ),
            ),
            logging.WARNING,
        )

    remux_result = run_remux(
        path,
        streams,
        [],
        _UNUSED_DOWNMIX_SETTINGS,
        ffmpeg_path=ffmpeg_path,
        timeout=remux_timeout,
        default_audio_index=winner_index,
        runner=runner,
    )
    if not remux_result.success:
        result = PipelineResult(
            outcome=PipelineOutcome.REMUX_FAILED,
            success=False,
            detail=(
                f"ffmpeg remux failed for {str(path)!r} "
                f"(exit code {remux_result.returncode}): {remux_result.stderr.strip()}"
            ),
            remux_result=remux_result,
        )
        # Same rationale as `run_downmix_pipeline` -- `result.detail` already
        # carries the *full*, untruncated stderr for job-history DB storage;
        # only the log line's tail is bounded.
        logger.error(
            "ffmpeg remux failed for %r (exit code %d) -- stderr (last %d chars): %s",
            str(path),
            remux_result.returncode,
            _STDERR_LOG_CHARS,
            _tail_for_log(remux_result.stderr),
        )
        return result

    try:
        # Only the *pre*-swap probes (of the original/temp, inside
        # apply_remux_result) can raise FfprobeError here -- the original is
        # still untouched if either does. The *post*-swap disposition
        # re-probe apply_remux_result also makes (via
        # expected_default_audio_index) never raises: its failure comes back
        # as an ApplyResult(failure_reason=ApplyFailureReason.
        # DISPOSITION_VERIFICATION_FAILED, ...) below instead, so it can't be
        # mistaken for this (safe, nothing-changed) pre-swap case.
        apply_result = apply_remux_result(
            path,
            remux_result,
            added_track_count=0,
            duration_tolerance_seconds=duration_tolerance_seconds,
            ffprobe_path=ffprobe_path,
            timeout=probe_timeout,
            runner=runner,
            expected_default_audio_index=winner_index,
        )
    except FfprobeError as exc:
        return _finish(
            PipelineResult(
                outcome=PipelineOutcome.APPLY_FAILED,
                success=False,
                detail=f"failed to validate remux result for {str(path)!r}: {exc}",
                remux_result=remux_result,
            ),
            logging.ERROR,
        )

    if not apply_result.success:
        return _finish(
            PipelineResult(
                outcome=PipelineOutcome.APPLY_FAILED,
                success=False,
                detail=apply_result.detail,
                remux_result=remux_result,
                apply_result=apply_result,
            ),
            logging.ERROR,
        )

    return PipelineResult(
        outcome=PipelineOutcome.SUCCESS,
        success=True,
        detail=apply_result.detail,
        remux_result=remux_result,
        apply_result=apply_result,
    )


def _index_of(streams: Sequence[AudioStreamInfo], winner: AudioStreamInfo) -> int:
    """Return ``winner``'s position within ``streams`` -- its output audio-relative index.

    ``streams`` is already audio-only (:func:`~collapsarr.downmix.probe.probe_audio_streams`
    selects only audio streams) and in ffprobe's original order, so position
    in this list is exactly the ``a:N`` index :func:`~collapsarr.downmix.remux.build_remux_command`
    numbers its ``-disposition:a:N`` flags by -- the same computation
    :func:`~collapsarr.downmix.default_audio.resolve_default_audio_output_index`
    makes over its own (longer, downmix-inclusive) final layout.
    """
    return next(i for i, stream in enumerate(streams) if stream is winner)


def _already_correct(streams: Sequence[AudioStreamInfo], winner_index: int) -> bool:
    """Whether ``streams[winner_index]`` already -- and solely -- carries the disposition.

    Mirrors :func:`~collapsarr.downmix.default_audio.resolve_default_audio_output_index`'s
    own "nothing to change" check: the winner must already be default *and*
    no other stream may wrongly carry the flag too (a file can, in principle,
    already have more than one stream marked default; that alone is still
    something worth fixing).
    """
    winner = streams[winner_index]
    return winner.is_default and not any(
        stream.is_default for i, stream in enumerate(streams) if i != winner_index
    )
