"""Tests for the disposition-only manual/bulk Default Audio Track pipeline (COL-153).

Two layers, mirroring ``tests/test_downmix_pipeline.py``:

- Real committed fixture media under ``tests/fixtures/downmix/`` driven
  through the actual ffmpeg/ffprobe binaries end-to-end: a genuine
  disposition change, two genuine no-ops (already correct; too few streams
  to compare), and a genuine validate-and-reject (an impossibly tight
  duration tolerance). Skipped when ffmpeg/ffprobe aren't installed.
- Injected-``runner`` unit tests: fast, binary-free coverage of the
  stage-by-stage control flow (probe failure, remux failure, apply failure —
  both duration and stream-count mismatch — and the full wiring on success),
  plus an explicit check that no new track is ever encoded.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from collapsarr.downmix.apply import ApplyFailureReason
from collapsarr.downmix.default_audio import DefaultAudioPreference
from collapsarr.downmix.default_audio_pipeline import run_default_audio_pipeline
from collapsarr.downmix.pipeline import PipelineOutcome
from collapsarr.downmix.targets import DownmixTarget

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "downmix"

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are not installed on this machine",
)


def _stream_summary(path: Path) -> list[tuple[str, str, int, int]]:
    """Return ``(codec_type, codec_name, channels-or-0, is_default)`` for every stream, in order."""
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    return [
        (
            s["codec_type"],
            s["codec_name"],
            s.get("channels", 0),
            s.get("disposition", {}).get("default", 0),
        )
        for s in payload["streams"]
    ]


# ---------------------------------------------------------------------------
# Real fixture media, through the actual ffmpeg/ffprobe binaries end-to-end.
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_pipeline_swaps_disposition_end_to_end_and_leaves_streams_otherwise_intact(
    tmp_path: Path,
) -> None:
    """multi_lang.mkv: eng stereo (currently default), fre 5.1 (not default)."""
    original = tmp_path / "movie.mkv"
    shutil.copy(FIXTURES_DIR / "multi_lang.mkv", original)
    before = _stream_summary(original)
    assert before == [("audio", "aac", 2, 1), ("audio", "ac3", 6, 0)]

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="fre", channel_tier=DownmixTarget.FIVE_POINT_ONE),
    )

    assert result.outcome is PipelineOutcome.SUCCESS
    assert result.success is True
    assert result.tracks_added == ()
    assert result.remux_result is not None and result.remux_result.success is True
    assert result.apply_result is not None and result.apply_result.success is True

    # Same streams, same codecs/channels, in the same order -- only the
    # disposition moved from the eng track to the fre track.
    after = _stream_summary(original)
    assert [(c, n, ch) for c, n, ch, _ in after] == [(c, n, ch) for c, n, ch, _ in before]
    assert after == [("audio", "aac", 2, 0), ("audio", "ac3", 6, 1)]
    # No leftover temp file -- the atomic swap consumed it.
    assert list(tmp_path.iterdir()) == [original]


@requires_ffmpeg
def test_pipeline_is_a_noop_when_disposition_already_matches(tmp_path: Path) -> None:
    """multi_lang.mkv: eng stereo is the exact match and is already default."""
    original = tmp_path / "movie.mkv"
    shutil.copy(FIXTURES_DIR / "multi_lang.mkv", original)
    original_bytes = original.read_bytes()

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
    )

    assert result.outcome is PipelineOutcome.NOTHING_TO_DO
    assert result.success is True
    assert result.remux_result is None
    assert result.apply_result is None
    assert "already" in result.detail
    # Byte-for-byte untouched, no temp file created, no swap performed.
    assert original.read_bytes() == original_bytes
    assert list(tmp_path.iterdir()) == [original]


@requires_ffmpeg
def test_pipeline_is_a_noop_with_a_single_audio_stream(tmp_path: Path) -> None:
    """stereo_eng.mkv has exactly one audio stream -- nothing to compare."""
    original = tmp_path / "movie.mkv"
    shutil.copy(FIXTURES_DIR / "stereo_eng.mkv", original)
    original_bytes = original.read_bytes()

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="jpn", channel_tier=DownmixTarget.FIVE_POINT_ONE),
    )

    assert result.outcome is PipelineOutcome.NOTHING_TO_DO
    assert result.success is True
    assert "nothing to do" in result.detail
    assert original.read_bytes() == original_bytes
    assert list(tmp_path.iterdir()) == [original]


@requires_ffmpeg
def test_pipeline_reports_apply_failure_and_leaves_original_untouched(tmp_path: Path) -> None:
    """A real (successful) remux that's still rejected by an impossibly tight tolerance."""
    original = tmp_path / "movie.mkv"
    shutil.copy(FIXTURES_DIR / "multi_lang.mkv", original)
    original_bytes = original.read_bytes()

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="fre", channel_tier=DownmixTarget.FIVE_POINT_ONE),
        duration_tolerance_seconds=0.0,
    )

    assert result.outcome is PipelineOutcome.APPLY_FAILED
    assert result.success is False
    assert result.remux_result is not None and result.remux_result.success is True
    assert result.apply_result is not None
    assert result.apply_result.success is False
    assert "duration mismatch" in result.detail
    # Original untouched, no orphaned temp file left by either stage.
    assert original.read_bytes() == original_bytes
    assert list(tmp_path.iterdir()) == [original]


# ---------------------------------------------------------------------------
# Injected-runner unit tests: stage-by-stage control flow.
# ---------------------------------------------------------------------------


def _fake_runner(
    *,
    original_path: Path,
    audio_payload: Mapping[str, object],
    post_swap_audio_payload: Mapping[str, object] | None = None,
    original_summary: tuple[float, int] = (10.0, 2),
    temp_summary: tuple[float, int] = (10.0, 2),
    probe_audio_returncode: int = 0,
    post_swap_probe_audio_returncode: int = 0,
    ffmpeg_returncode: int = 0,
    ffmpeg_stderr: str = "",
    media_summary_returncode: int = 0,
    calls: list[list[str]] | None = None,
) -> object:
    """A stub subprocess runner dispatching on binary + flags, like a fake ffmpeg/ffprobe.

    ``post_swap_audio_payload``/``post_swap_probe_audio_returncode`` (COL-241)
    answer the pipeline's *second* ``probe_audio_streams``-shaped call -- the
    post-swap disposition re-probe :func:`~collapsarr.downmix.apply.
    apply_remux_result` makes when given an ``expected_default_audio_index``
    -- separately from the first (pre-remux) one, which always gets
    ``audio_payload``/``probe_audio_returncode``. Both default to mirroring
    the first call's values, so tests that don't care about the disposition
    check (or want it to trivially pass with an unchanged stream list) don't
    need to pass either.
    """
    audio_probe_calls = 0

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal audio_probe_calls
        if calls is not None:
            calls.append(list(command))
        binary = command[0]
        if "ffprobe" in binary and "-show_format" in command:
            path = command[-1]
            if media_summary_returncode != 0:
                return subprocess.CompletedProcess(
                    list(command), media_summary_returncode, "", "Invalid data found"
                )
            duration, count = original_summary if path == str(original_path) else temp_summary
            payload = {"format": {"duration": str(duration)}, "streams": [{}] * count}
            return subprocess.CompletedProcess(list(command), 0, json.dumps(payload), "")
        if "ffprobe" in binary:
            audio_probe_calls += 1
            is_first_call = audio_probe_calls == 1
            audio_probe_payload = (
                audio_payload
                if is_first_call or post_swap_audio_payload is None
                else post_swap_audio_payload
            )
            returncode = (
                probe_audio_returncode if is_first_call else post_swap_probe_audio_returncode
            )
            stderr = "Invalid data found" if returncode != 0 else ""
            return subprocess.CompletedProcess(
                list(command), returncode, json.dumps(audio_probe_payload), stderr
            )
        if "ffmpeg" in binary:
            return subprocess.CompletedProcess(list(command), ffmpeg_returncode, "", ffmpeg_stderr)
        raise AssertionError(f"unexpected command: {command}")

    return runner


# eng 6ch (currently default) + eng 2ch (not default) -- a preference for
# eng Stereo needs a real disposition change.
_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD = {
    "streams": [
        {
            "index": 0,
            "codec_type": "audio",
            "codec_name": "ac3",
            "channels": 6,
            "channel_layout": "5.1",
            "tags": {"language": "eng"},
            "disposition": {"default": 1},
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "channel_layout": "stereo",
            "tags": {"language": "eng"},
            "disposition": {"default": 0},
        },
    ]
}

# Same two streams as `_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD`, but with the
# disposition flipped onto the stereo track (a:1) -- the post-swap state a
# genuine, correct remux produces when the preference picks eng Stereo. Used
# to answer `_fake_runner`'s post-swap re-probe (COL-241) on the success path.
_ENG_STEREO_DEFAULT_PLUS_5_1_PAYLOAD = {
    "streams": [
        {
            "index": 0,
            "codec_type": "audio",
            "codec_name": "ac3",
            "channels": 6,
            "channel_layout": "5.1",
            "tags": {"language": "eng"},
            "disposition": {"default": 0},
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "channel_layout": "stereo",
            "tags": {"language": "eng"},
            "disposition": {"default": 1},
        },
    ]
}

# A *broken* remux: the disposition never actually moved (both post-swap
# streams still read exactly as they did pre-remux -- a:0 still default,
# a:1 still not) even though the preference picked eng Stereo (a:1). Used to
# drive the disposition-mismatch failure path (COL-241).
_UNCHANGED_DISPOSITION_PAYLOAD = _ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD

_SINGLE_STREAM_PAYLOAD = {
    "streams": [
        {
            "index": 0,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "channel_layout": "stereo",
            "tags": {"language": "eng"},
            "disposition": {"default": 0},
        }
    ]
}


def _ffmpeg_command(calls: list[list[str]]) -> list[str]:
    ffmpeg_calls = [c for c in calls if "ffmpeg" in c[0]]
    assert len(ffmpeg_calls) == 1, f"expected one ffmpeg call, got {len(ffmpeg_calls)}"
    return ffmpeg_calls[0]


def test_pipeline_succeeds_with_injected_runner_and_wires_all_stages(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    runner = _fake_runner(
        original_path=original,
        audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD,
        post_swap_audio_payload=_ENG_STEREO_DEFAULT_PLUS_5_1_PAYLOAD,
        calls=calls,
    )

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.outcome is PipelineOutcome.SUCCESS
    assert result.success is True
    assert result.tracks_added == ()
    assert result.remux_result is not None and result.remux_result.success is True
    assert result.apply_result is not None and result.apply_result.success is True
    # COL-241: the post-swap re-probe confirmed the disposition really moved.
    assert "disposition verified" in result.apply_result.detail
    binaries = [c[0] for c in calls]
    assert binaries == ["ffprobe", "ffmpeg", "ffprobe", "ffprobe", "ffprobe"]
    assert original.exists()

    # The disposition moved onto the eng stereo stream (a:1); the wrongly
    # -- now -- defaulted 5.1 (a:0) is cleared. No new track was encoded: the
    # command only carries the blanket `-map 0`, no `-c:a:N` overrides.
    command = _ffmpeg_command(calls)
    map_indices = [command[i + 1] for i, arg in enumerate(command) if arg == "-map"]
    assert map_indices == ["0"]
    assert not any(arg.startswith("-c:a:") for arg in command)
    assert command[command.index("-disposition:a:0") + 1] == "0"
    assert command[command.index("-disposition:a:1") + 1] == "default"


def test_pipeline_reports_apply_failure_on_disposition_mismatch(tmp_path: Path) -> None:
    """The swap happened, but the resulting file's disposition never actually moved."""
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    runner = _fake_runner(
        original_path=original,
        audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD,
        # A buggy/no-op remux: post-swap disposition is unchanged from
        # pre-swap -- a:0 (5.1) is still (wrongly) default, a:1 (stereo,
        # the expected winner) still isn't.
        post_swap_audio_payload=_UNCHANGED_DISPOSITION_PAYLOAD,
        calls=calls,
    )

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.outcome is PipelineOutcome.APPLY_FAILED
    assert result.success is False
    assert result.remux_result is not None and result.remux_result.success is True
    assert result.apply_result is not None
    assert result.apply_result.success is False
    assert result.apply_result.failure_reason is ApplyFailureReason.DISPOSITION_MISMATCH
    assert "disposition mismatch" in result.detail
    # All five probes/ffmpeg calls still ran -- the mismatch is only caught
    # by the final, post-swap re-probe.
    binaries = [c[0] for c in calls]
    assert binaries == ["ffprobe", "ffmpeg", "ffprobe", "ffprobe", "ffprobe"]


def test_pipeline_reports_apply_failure_when_post_swap_reprobe_itself_errors(
    tmp_path: Path,
) -> None:
    """The swap already happened; the post-swap re-probe fails outright (not a mismatch).

    Distinct from `test_pipeline_reports_apply_failure_on_disposition_mismatch`
    above (re-probe *completes* and finds the wrong result) and from
    `test_pipeline_reports_apply_failure_when_validation_probing_fails` below
    (a *pre*-swap probe failure, where the original is untouched) -- this is
    the third, previously-conflated case: the swap happened and verification
    itself never completed.
    """
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    runner = _fake_runner(
        original_path=original,
        audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD,
        post_swap_probe_audio_returncode=1,
        calls=calls,
    )

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.outcome is PipelineOutcome.APPLY_FAILED
    assert result.success is False
    assert result.remux_result is not None and result.remux_result.success is True
    assert result.apply_result is not None
    assert result.apply_result.success is False
    assert result.apply_result.failure_reason is ApplyFailureReason.DISPOSITION_VERIFICATION_FAILED
    assert "disposition verification failed" in result.detail
    binaries = [c[0] for c in calls]
    assert binaries == ["ffprobe", "ffmpeg", "ffprobe", "ffprobe", "ffprobe"]


def test_pipeline_is_a_noop_when_disposition_already_matches_with_injected_runner(
    tmp_path: Path,
) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    runner = _fake_runner(
        original_path=original,
        audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD,
        calls=calls,
    )

    result = run_default_audio_pipeline(
        original,
        # Winner is the eng 5.1 track (index 0), already the only default.
        DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE),
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.outcome is PipelineOutcome.NOTHING_TO_DO
    assert result.success is True
    assert result.remux_result is None
    assert result.apply_result is None
    # Only the (nothing-to-change) audio-stream probe ran -- no ffmpeg, no
    # post-remux validation probes.
    assert len(calls) == 1


def test_pipeline_is_a_noop_with_a_single_stream_and_injected_runner(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    runner = _fake_runner(
        original_path=original, audio_payload=_SINGLE_STREAM_PAYLOAD, calls=calls
    )

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="jpn", channel_tier=DownmixTarget.FIVE_POINT_ONE),
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.outcome is PipelineOutcome.NOTHING_TO_DO
    assert result.success is True
    assert len(calls) == 1


def test_pipeline_reports_probe_failure_without_running_remux_or_apply(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    runner = _fake_runner(
        original_path=original,
        audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD,
        probe_audio_returncode=1,
        calls=calls,
    )

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.outcome is PipelineOutcome.PROBE_FAILED
    assert result.success is False
    assert result.remux_result is None
    assert result.apply_result is None
    assert str(original) in result.detail
    assert len(calls) == 1


def test_pipeline_reports_remux_failure_without_running_apply(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    runner = _fake_runner(
        original_path=original,
        audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD,
        ffmpeg_returncode=1,
        ffmpeg_stderr="Invalid data found when processing input",
        calls=calls,
    )

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.outcome is PipelineOutcome.REMUX_FAILED
    assert result.success is False
    assert result.remux_result is not None
    assert result.remux_result.success is False
    assert result.remux_result.returncode == 1
    assert "Invalid data found" in result.detail
    assert result.apply_result is None
    binaries = [c[0] for c in calls]
    assert binaries == ["ffprobe", "ffmpeg"]


def test_pipeline_reports_apply_failure_on_duration_mismatch(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    runner = _fake_runner(
        original_path=original,
        audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD,
        original_summary=(10.0, 2),
        temp_summary=(15.0, 2),  # duration drifted far past tolerance
    )

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.outcome is PipelineOutcome.APPLY_FAILED
    assert result.success is False
    assert result.remux_result is not None and result.remux_result.success is True
    assert result.apply_result is not None
    assert result.apply_result.success is False
    assert "duration mismatch" in result.detail


def test_pipeline_reports_apply_failure_on_stream_count_mismatch(tmp_path: Path) -> None:
    """A disposition-only remux must produce exactly the same stream count."""
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    runner = _fake_runner(
        original_path=original,
        audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD,
        original_summary=(10.0, 2),
        temp_summary=(10.0, 3),  # a stream was silently added/dropped
    )

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.outcome is PipelineOutcome.APPLY_FAILED
    assert result.success is False
    assert result.remux_result is not None and result.remux_result.success is True
    assert result.apply_result is not None
    assert result.apply_result.success is False
    assert "stream-count mismatch" in result.detail


def test_pipeline_reports_apply_failure_when_validation_probing_fails(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    runner = _fake_runner(
        original_path=original,
        audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD,
        media_summary_returncode=1,
    )

    result = run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.outcome is PipelineOutcome.APPLY_FAILED
    assert result.success is False
    assert result.remux_result is not None and result.remux_result.success is True
    assert result.apply_result is None
    assert str(original) in result.detail


def test_pipeline_passes_ffprobe_ffmpeg_paths_and_timeouts_through(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    runner = _fake_runner(
        original_path=original, audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD, calls=calls
    )

    run_default_audio_pipeline(
        original,
        DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
        ffprobe_path="/opt/homebrew/bin/ffprobe",
        ffmpeg_path="/opt/homebrew/bin/ffmpeg",
        runner=runner,  # type: ignore[arg-type]
    )

    assert calls[0][0] == "/opt/homebrew/bin/ffprobe"
    assert calls[1][0] == "/opt/homebrew/bin/ffmpeg"
    assert calls[2][0] == "/opt/homebrew/bin/ffprobe"


# ---------------------------------------------------------------------------
# Lifecycle logging (COL-129): WARNING for skip/no-op, ERROR for failures.
# ---------------------------------------------------------------------------


def test_pipeline_logs_a_warning_for_a_single_stream_noop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    runner = _fake_runner(original_path=original, audio_payload=_SINGLE_STREAM_PAYLOAD)

    with caplog.at_level(logging.INFO, logger="collapsarr"):
        result = run_default_audio_pipeline(
            original,
            DefaultAudioPreference(language="jpn", channel_tier=DownmixTarget.FIVE_POINT_ONE),
            runner=runner,  # type: ignore[arg-type]
        )

    assert result.outcome is PipelineOutcome.NOTHING_TO_DO
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "nothing to do" in warnings[0].message
    assert str(original) in warnings[0].message


def test_pipeline_logs_a_warning_when_disposition_already_matches(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    runner = _fake_runner(
        original_path=original, audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD
    )

    with caplog.at_level(logging.INFO, logger="collapsarr"):
        result = run_default_audio_pipeline(
            original,
            DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE),
            runner=runner,  # type: ignore[arg-type]
        )

    assert result.outcome is PipelineOutcome.NOTHING_TO_DO
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "already" in warnings[0].message


def test_pipeline_logs_an_error_with_truncated_stderr_for_a_remux_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    huge_stderr = "x" * 3000 + "THE_REAL_ERROR_AT_THE_END"
    runner = _fake_runner(
        original_path=original,
        audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD,
        ffmpeg_returncode=1,
        ffmpeg_stderr=huge_stderr,
    )

    with caplog.at_level(logging.INFO, logger="collapsarr"):
        result = run_default_audio_pipeline(
            original,
            DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
            runner=runner,  # type: ignore[arg-type]
        )

    assert result.outcome is PipelineOutcome.REMUX_FAILED
    assert result.remux_result is not None
    assert result.remux_result.stderr == huge_stderr

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    logged_message = errors[0].message
    assert "THE_REAL_ERROR_AT_THE_END" in logged_message
    assert "x" * 3000 not in logged_message


def test_pipeline_logs_an_error_for_a_probe_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    runner = _fake_runner(
        original_path=original,
        audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD,
        probe_audio_returncode=1,
    )

    with caplog.at_level(logging.INFO, logger="collapsarr"):
        result = run_default_audio_pipeline(
            original,
            DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
            runner=runner,  # type: ignore[arg-type]
        )

    assert result.outcome is PipelineOutcome.PROBE_FAILED
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert str(original) in errors[0].message


def test_pipeline_logs_an_error_for_an_apply_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    runner = _fake_runner(
        original_path=original,
        audio_payload=_ENG_5_1_DEFAULT_PLUS_STEREO_PAYLOAD,
        original_summary=(10.0, 2),
        temp_summary=(15.0, 2),
    )

    with caplog.at_level(logging.INFO, logger="collapsarr"):
        result = run_default_audio_pipeline(
            original,
            DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.STEREO),
            runner=runner,  # type: ignore[arg-type]
        )

    assert result.outcome is PipelineOutcome.APPLY_FAILED
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "duration mismatch" in errors[0].message
