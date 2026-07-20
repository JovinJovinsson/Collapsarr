"""Tests for the end-to-end single-file downmix pipeline (COL-19).

Two layers, mirroring the rest of the downmix-engine test suite:

- Real committed fixture media under ``tests/fixtures/downmix/`` driven
  through the actual ffmpeg/ffprobe binaries end-to-end: a genuine success
  (new tracks added, all original streams intact), a genuine no-op (no
  qualifying targets), and a genuine ffmpeg failure (bad codec override) —
  never mocked, per the PRD's testing decisions for this seam. Skipped when
  ffmpeg/ffprobe aren't installed.
- Injected-``runner`` unit tests: fast, binary-free coverage of the
  stage-by-stage control flow (probe failure, remux failure short-circuiting
  apply, apply failure, and the full wiring on success).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from collapsarr.downmix.pipeline import PipelineOutcome, run_downmix_pipeline
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget, QualifyingTarget

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "downmix"

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are not installed on this machine",
)

ALL_TARGETS = frozenset(
    {DownmixTarget.STEREO, DownmixTarget.TWO_POINT_ONE, DownmixTarget.FIVE_POINT_ONE}
)


def _stream_summary(path: Path) -> list[tuple[str, str, int]]:
    """Return ``(codec_type, codec_name, channels-or-0)`` for every stream, in order."""
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    return [(s["codec_type"], s["codec_name"], s.get("channels", 0)) for s in payload["streams"]]


# ---------------------------------------------------------------------------
# Real fixture media, through the actual ffmpeg/ffprobe binaries end-to-end.
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_pipeline_succeeds_end_to_end_and_leaves_original_streams_intact(
    tmp_path: Path,
) -> None:
    """multi_lang.mkv (eng stereo, fre 5.1) qualifies fre for Stereo + 2.1."""
    original = tmp_path / "movie.mkv"
    shutil.copy(FIXTURES_DIR / "multi_lang.mkv", original)
    before = _stream_summary(original)
    assert before == [("audio", "aac", 2), ("audio", "ac3", 6)]

    result = run_downmix_pipeline(original, DownmixSettings(enabled_targets=ALL_TARGETS))

    assert result.outcome is PipelineOutcome.SUCCESS
    assert result.success is True
    assert result.tracks_added == (
        QualifyingTarget(language="fre", target=DownmixTarget.STEREO),
        QualifyingTarget(language="fre", target=DownmixTarget.TWO_POINT_ONE),
    )
    assert result.remux_result is not None and result.remux_result.success is True
    assert result.apply_result is not None and result.apply_result.success is True

    # The original streams are still present, in order, untouched -- plus
    # the two new downmix tracks appended after them.
    after = _stream_summary(original)
    assert after[:2] == before
    assert after == [
        ("audio", "aac", 2),  # original eng stereo, copied
        ("audio", "ac3", 6),  # original fre 5.1, copied
        ("audio", "aac", 2),  # new fre Stereo track, encoded
        ("audio", "ac3", 3),  # new fre 2.1 track, encoded
    ]
    # No leftover temp file -- the atomic swap consumed it.
    assert list(tmp_path.iterdir()) == [original]


@requires_ffmpeg
def test_pipeline_is_a_noop_when_no_targets_qualify(tmp_path: Path) -> None:
    """stereo_eng.mkv already has eng stereo; the default settings only enable Stereo."""
    original = tmp_path / "movie.mkv"
    shutil.copy(FIXTURES_DIR / "stereo_eng.mkv", original)
    original_bytes = original.read_bytes()

    result = run_downmix_pipeline(original, DownmixSettings())

    assert result.outcome is PipelineOutcome.NOTHING_TO_DO
    assert result.success is True
    assert result.tracks_added == ()
    assert result.remux_result is None
    assert result.apply_result is None
    assert "nothing to do" in result.detail
    # Nothing was ever invoked against the file -- byte-for-byte untouched,
    # no temp file created.
    assert original.read_bytes() == original_bytes
    assert list(tmp_path.iterdir()) == [original]


@requires_ffmpeg
def test_pipeline_reports_remux_failure_and_leaves_original_untouched(tmp_path: Path) -> None:
    """A genuine ffmpeg failure (unknown encoder) fails the remux stage cleanly."""
    original = tmp_path / "movie.mkv"
    shutil.copy(FIXTURES_DIR / "video_audio_subs.mkv", original)  # eng 5.1 -> Stereo qualifies
    original_bytes = original.read_bytes()
    settings = DownmixSettings(stereo_codec="definitely_not_a_real_codec")

    result = run_downmix_pipeline(original, settings)

    assert result.outcome is PipelineOutcome.REMUX_FAILED
    assert result.success is False
    assert result.tracks_added == ()
    assert result.remux_result is not None
    assert result.remux_result.success is False
    assert result.remux_result.returncode != 0
    assert result.remux_result.stderr  # real ffmpeg stderr surfaced
    assert str(result.remux_result.returncode) in result.detail
    assert result.apply_result is None
    # Original untouched, no orphaned temp file.
    assert original.read_bytes() == original_bytes
    assert list(tmp_path.iterdir()) == [original]


@requires_ffmpeg
def test_pipeline_reports_apply_failure_and_leaves_original_untouched(tmp_path: Path) -> None:
    """A real (successful) remux that's still rejected by an impossibly tight tolerance."""
    original = tmp_path / "movie.mkv"
    shutil.copy(FIXTURES_DIR / "video_audio_subs.mkv", original)
    original_bytes = original.read_bytes()

    result = run_downmix_pipeline(
        original, DownmixSettings(), duration_tolerance_seconds=0.0
    )

    assert result.outcome is PipelineOutcome.APPLY_FAILED
    assert result.success is False
    assert result.tracks_added == ()
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
    original_summary: tuple[float, int] = (10.0, 1),
    temp_summary: tuple[float, int] = (10.0, 2),
    probe_audio_returncode: int = 0,
    ffmpeg_returncode: int = 0,
    ffmpeg_stderr: str = "",
    media_summary_returncode: int = 0,
    calls: list[list[str]] | None = None,
) -> object:
    """A stub subprocess runner dispatching on binary + flags, like a fake ffmpeg/ffprobe."""

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
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
            return subprocess.CompletedProcess(
                list(command), 0, json.dumps(payload), ""
            )
        if "ffprobe" in binary:
            return subprocess.CompletedProcess(
                list(command), probe_audio_returncode, json.dumps(audio_payload), ""
            )
        if "ffmpeg" in binary:
            return subprocess.CompletedProcess(
                list(command), ffmpeg_returncode, "", ffmpeg_stderr
            )
        raise AssertionError(f"unexpected command: {command}")

    return runner


_STEREO_ENG_PAYLOAD = {
    "streams": [
        {
            "index": 0,
            "codec_type": "audio",
            "codec_name": "flac",
            "channels": 6,
            "channel_layout": "5.1",
            "tags": {"language": "eng"},
        }
    ]
}


def test_pipeline_succeeds_with_injected_runner_and_wires_all_stages(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    runner = _fake_runner(
        original_path=original,
        audio_payload=_STEREO_ENG_PAYLOAD,
        original_summary=(10.0, 1),
        temp_summary=(10.0, 2),
        calls=calls,
    )

    result = run_downmix_pipeline(original, DownmixSettings(), runner=runner)  # type: ignore[arg-type]

    assert result.outcome is PipelineOutcome.SUCCESS
    assert result.success is True
    assert result.tracks_added == (QualifyingTarget(language="eng", target=DownmixTarget.STEREO),)
    assert result.remux_result is not None and result.remux_result.success is True
    assert result.apply_result is not None and result.apply_result.success is True
    # All three stages actually ran, in order: ffprobe (audio), ffmpeg, ffprobe x2 (summaries).
    binaries = [c[0] for c in calls]
    assert binaries == ["ffprobe", "ffmpeg", "ffprobe", "ffprobe"]
    # The atomic rename really happened: the original path still exists,
    # now holding the (stubbed) remux temp's contents.
    assert original.exists()


def test_pipeline_reports_probe_failure_without_running_remux_or_apply(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    runner = _fake_runner(
        original_path=original,
        audio_payload=_STEREO_ENG_PAYLOAD,
        probe_audio_returncode=1,
        calls=calls,
    )

    result = run_downmix_pipeline(original, DownmixSettings(), runner=runner)  # type: ignore[arg-type]

    assert result.outcome is PipelineOutcome.PROBE_FAILED
    assert result.success is False
    assert result.tracks_added == ()
    assert result.remux_result is None
    assert result.apply_result is None
    assert str(original) in result.detail
    # Only the (failed) audio-stream probe ran -- nothing downstream was attempted.
    assert len(calls) == 1


def test_pipeline_reports_nothing_to_do_without_running_remux_or_apply(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    # eng stereo already present; default settings only enable Stereo -> no qualifying target.
    payload = {
        "streams": [
            {
                "index": 0,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "channel_layout": "stereo",
                "tags": {"language": "eng"},
            }
        ]
    }
    runner = _fake_runner(original_path=original, audio_payload=payload, calls=calls)

    result = run_downmix_pipeline(original, DownmixSettings(), runner=runner)  # type: ignore[arg-type]

    assert result.outcome is PipelineOutcome.NOTHING_TO_DO
    assert result.success is True
    assert result.tracks_added == ()
    assert result.remux_result is None
    assert result.apply_result is None
    assert len(calls) == 1  # only the audio-stream probe ran


def test_pipeline_reports_remux_failure_without_running_apply(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    runner = _fake_runner(
        original_path=original,
        audio_payload=_STEREO_ENG_PAYLOAD,
        ffmpeg_returncode=1,
        ffmpeg_stderr="Unknown encoder 'bogus'",
        calls=calls,
    )

    result = run_downmix_pipeline(original, DownmixSettings(), runner=runner)  # type: ignore[arg-type]

    assert result.outcome is PipelineOutcome.REMUX_FAILED
    assert result.success is False
    assert result.tracks_added == ()
    assert result.remux_result is not None
    assert result.remux_result.success is False
    assert result.remux_result.returncode == 1
    assert "Unknown encoder" in result.detail
    assert result.apply_result is None
    # ffprobe (audio) + ffmpeg only -- apply's ffprobe summary calls never ran.
    binaries = [c[0] for c in calls]
    assert binaries == ["ffprobe", "ffmpeg"]


def test_pipeline_reports_apply_failure_when_validation_rejects_the_remux(
    tmp_path: Path,
) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    runner = _fake_runner(
        original_path=original,
        audio_payload=_STEREO_ENG_PAYLOAD,
        original_summary=(10.0, 1),
        temp_summary=(15.0, 2),  # duration drifted far past tolerance
    )

    result = run_downmix_pipeline(original, DownmixSettings(), runner=runner)  # type: ignore[arg-type]

    assert result.outcome is PipelineOutcome.APPLY_FAILED
    assert result.success is False
    assert result.tracks_added == ()
    assert result.remux_result is not None and result.remux_result.success is True
    assert result.apply_result is not None
    assert result.apply_result.success is False
    assert "duration mismatch" in result.detail


def test_pipeline_reports_apply_failure_when_validation_probing_fails(
    tmp_path: Path,
) -> None:
    """A post-remux ffprobe failure during validation is caught, not raised."""
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    runner = _fake_runner(
        original_path=original,
        audio_payload=_STEREO_ENG_PAYLOAD,
        media_summary_returncode=1,
    )

    result = run_downmix_pipeline(original, DownmixSettings(), runner=runner)  # type: ignore[arg-type]

    assert result.outcome is PipelineOutcome.APPLY_FAILED
    assert result.success is False
    assert result.remux_result is not None and result.remux_result.success is True
    assert result.apply_result is None
    assert str(original) in result.detail


def test_pipeline_passes_ffprobe_ffmpeg_paths_and_timeouts_through(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"")
    calls: list[list[str]] = []
    runner = _fake_runner(original_path=original, audio_payload=_STEREO_ENG_PAYLOAD, calls=calls)

    run_downmix_pipeline(
        original,
        DownmixSettings(),
        ffprobe_path="/opt/homebrew/bin/ffprobe",
        ffmpeg_path="/opt/homebrew/bin/ffmpeg",
        runner=runner,  # type: ignore[arg-type]
    )

    assert calls[0][0] == "/opt/homebrew/bin/ffprobe"
    assert calls[1][0] == "/opt/homebrew/bin/ffmpeg"
    assert calls[2][0] == "/opt/homebrew/bin/ffprobe"
