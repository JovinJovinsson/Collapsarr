"""Tests for validate-and-atomically-apply of a remux result (COL-18).

Two layers, mirroring ``tests/test_downmix_remux.py``:

- Real committed fixture media under ``tests/fixtures/downmix/`` driven
  through the actual ffmpeg/ffprobe binaries end-to-end, covering the success
  swap and *each* failure path with genuinely-bad temp files — a truncated
  temp for the duration check, a stream-dropped temp for the stream-count
  check (never mocked, since this is the safety-critical path). Every such
  test operates on a copy in ``tmp_path`` so the committed fixture is never
  mutated. Skipped if ffmpeg isn't installed.
- Injected-``runner`` unit tests: fast, binary-free coverage of the
  validation/decision logic and the temp-file lifecycle, with a real
  ``Path.rename`` on disk so the atomic-swap behaviour itself is exercised.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from collapsarr.downmix.apply import (
    DEFAULT_DURATION_TOLERANCE_SECONDS,
    ApplyFailureReason,
    apply_remux_result,
)
from collapsarr.downmix.probe import FfprobeError, probe_audio_streams
from collapsarr.downmix.remux import RemuxResult, run_remux
from collapsarr.downmix.targets import (
    DownmixSettings,
    DownmixTarget,
    detect_qualifying_targets,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "downmix"

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are not installed on this machine",
)

ALL_TARGETS = frozenset(
    {DownmixTarget.STEREO, DownmixTarget.TWO_POINT_ONE, DownmixTarget.FIVE_POINT_ONE}
)


def _stream_count(path: Path) -> int:
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return len(json.loads(proc.stdout)["streams"])


# ---------------------------------------------------------------------------
# Real fixture media, through the actual ffmpeg/ffprobe binaries end-to-end.
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_apply_swaps_validated_remux_over_original_and_leaves_no_backup(
    tmp_path: Path,
) -> None:
    """A genuine remux that adds 2 fre tracks validates and atomically replaces the original."""
    original = tmp_path / "movie.mkv"
    shutil.copy(FIXTURES_DIR / "multi_lang.mkv", original)  # eng stereo + fre 5.1
    original_bytes = original.read_bytes()

    streams = probe_audio_streams(original)
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)
    targets = detect_qualifying_targets(streams, settings)  # fre -> Stereo + 2.1
    assert len(targets) == 2

    remux = run_remux(original, streams, targets, settings)
    assert remux.success is True
    temp = remux.temp_file_path
    assert temp is not None

    result = apply_remux_result(original, remux, added_track_count=len(targets))

    assert result.success is True
    assert result.failure_reason is None
    assert result.applied_path == original
    # The original path now holds the remuxed file (4 streams), swapped in.
    assert original.exists()
    assert original.read_bytes() != original_bytes
    assert _stream_count(original) == 4
    # Temp consumed by the rename; no backup of the original retained anywhere.
    assert not temp.exists()
    assert list(tmp_path.iterdir()) == [original]


@requires_ffmpeg
def test_apply_rejects_duration_mismatch_and_leaves_original_untouched(
    tmp_path: Path,
) -> None:
    """A temp whose duration drifts past tolerance is discarded; the original is intact."""
    original = tmp_path / "movie.mkv"
    shutil.copy(FIXTURES_DIR / "multi_lang.mkv", original)
    original_bytes = original.read_bytes()

    streams = probe_audio_streams(original)
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)
    targets = detect_qualifying_targets(streams, settings)
    remux = run_remux(original, streams, targets, settings)
    assert remux.success is True and remux.temp_file_path is not None
    temp = remux.temp_file_path

    # The temp is a genuine, correctly-structured remux (4 streams). Drive the
    # duration check with a punishingly tight tolerance so the real ~tens-of-ms
    # re-encode drift trips it, isolating the duration path from stream count.
    result = apply_remux_result(
        original, remux, added_track_count=len(targets), duration_tolerance_seconds=0.001
    )

    assert result.success is False
    assert result.failure_reason is ApplyFailureReason.DURATION_MISMATCH
    assert result.applied_path is None
    # Original byte-for-byte unchanged; temp discarded; no backup left behind.
    assert original.read_bytes() == original_bytes
    assert not temp.exists()
    assert list(tmp_path.iterdir()) == [original]


@requires_ffmpeg
def test_apply_rejects_stream_count_mismatch_and_leaves_original_untouched(
    tmp_path: Path,
) -> None:
    """A temp that silently dropped a stream is discarded; the original is intact."""
    original = tmp_path / "movie.mkv"
    shutil.copy(FIXTURES_DIR / "multi_lang.mkv", original)  # 2 audio streams
    original_bytes = original.read_bytes()

    # Fabricate a *broken* remux temp in the same directory: keep only one
    # audio stream (a faulty remux that lost a stream). Duration stays ~equal
    # to the original, so only the stream-count check can trip.
    bad_temp = tmp_path / ".movie.broken.collapsarr-tmp.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(original),
            "-map",
            "0:a:0",
            "-c",
            "copy",
            str(bad_temp),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert _stream_count(bad_temp) == 1
    remux = RemuxResult(success=True, temp_file_path=bad_temp, returncode=0, stderr="")

    # Two audio streams originally + 2 tracks expected => expect 4, temp has 1.
    result = apply_remux_result(original, remux, added_track_count=2)

    assert result.success is False
    assert result.failure_reason is ApplyFailureReason.STREAM_COUNT_MISMATCH
    assert result.applied_path is None
    assert original.read_bytes() == original_bytes
    assert not bad_temp.exists()
    assert list(tmp_path.iterdir()) == [original]


# ---------------------------------------------------------------------------
# Injected-runner unit tests: validation/decision logic + swap lifecycle.
# ---------------------------------------------------------------------------


def _summary_runner(
    by_path: dict[str, tuple[float, int]],
    *,
    fail_paths: frozenset[str] = frozenset(),
) -> object:
    """Return a runner that answers ffprobe with canned (duration, stream_count) per path."""

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        path = command[-1]
        if path in fail_paths:
            return subprocess.CompletedProcess(
                args=list(command), returncode=1, stdout="", stderr="Invalid data"
            )
        duration, count = by_path[path]
        payload = {"format": {"duration": str(duration)}, "streams": [{}] * count}
        return subprocess.CompletedProcess(
            args=list(command), returncode=0, stdout=json.dumps(payload), stderr=""
        )

    return runner


def _make_files(tmp_path: Path) -> tuple[Path, Path, RemuxResult]:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"ORIGINAL")
    temp = tmp_path / ".movie.abc.collapsarr-tmp.mkv"
    temp.write_bytes(b"REMUXED-TEMP")
    remux = RemuxResult(success=True, temp_file_path=temp, returncode=0, stderr="")
    return original, temp, remux


def test_apply_swaps_temp_over_original_on_matching_duration_and_stream_count(
    tmp_path: Path,
) -> None:
    original, temp, remux = _make_files(tmp_path)
    runner = _summary_runner({str(original): (100.0, 3), str(temp): (100.05, 4)})

    result = apply_remux_result(original, remux, added_track_count=1, runner=runner)  # type: ignore[arg-type]

    assert result.success is True
    assert result.applied_path == original
    # The atomic rename really happened: original now holds the temp's bytes.
    assert original.read_bytes() == b"REMUXED-TEMP"
    assert not temp.exists()
    assert list(tmp_path.iterdir()) == [original]  # no backup retained


def test_apply_accepts_duration_delta_exactly_at_tolerance(tmp_path: Path) -> None:
    """Delta == tolerance is within tolerance (only a strictly greater delta fails)."""
    original, temp, remux = _make_files(tmp_path)
    runner = _summary_runner({str(original): (10.0, 1), str(temp): (10.5, 2)})

    result = apply_remux_result(
        original,
        remux,
        added_track_count=1,
        duration_tolerance_seconds=0.5,
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.success is True


def test_apply_rejects_duration_mismatch_deletes_temp_keeps_original(tmp_path: Path) -> None:
    original, temp, remux = _make_files(tmp_path)
    runner = _summary_runner({str(original): (100.0, 3), str(temp): (100.6, 4)})

    result = apply_remux_result(
        original,
        remux,
        added_track_count=1,
        duration_tolerance_seconds=0.5,
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert result.failure_reason is ApplyFailureReason.DURATION_MISMATCH
    assert "duration mismatch" in result.detail
    assert original.read_bytes() == b"ORIGINAL"  # untouched
    assert not temp.exists()
    assert list(tmp_path.iterdir()) == [original]


def test_apply_rejects_stream_count_mismatch_deletes_temp_keeps_original(
    tmp_path: Path,
) -> None:
    original, temp, remux = _make_files(tmp_path)
    # Durations match, but temp has 5 streams where 3 + 1 = 4 were expected.
    runner = _summary_runner({str(original): (100.0, 3), str(temp): (100.0, 5)})

    result = apply_remux_result(original, remux, added_track_count=1, runner=runner)  # type: ignore[arg-type]

    assert result.success is False
    assert result.failure_reason is ApplyFailureReason.STREAM_COUNT_MISMATCH
    assert "stream-count mismatch" in result.detail
    assert original.read_bytes() == b"ORIGINAL"
    assert not temp.exists()
    assert list(tmp_path.iterdir()) == [original]


def test_apply_uses_default_tolerance_when_unspecified(tmp_path: Path) -> None:
    """The documented default tolerance admits a sub-second re-encode drift."""
    original, temp, remux = _make_files(tmp_path)
    # A delta comfortably inside the default but well over the tight test value.
    delta = DEFAULT_DURATION_TOLERANCE_SECONDS - 0.05
    runner = _summary_runner({str(original): (50.0, 2), str(temp): (50.0 + delta, 3)})

    result = apply_remux_result(original, remux, added_track_count=1, runner=runner)  # type: ignore[arg-type]

    assert result.success is True


def test_apply_on_probe_failure_deletes_temp_and_leaves_original(tmp_path: Path) -> None:
    """If the temp can't be probed, we never swap, never orphan the temp, keep the original."""
    original, temp, remux = _make_files(tmp_path)
    runner = _summary_runner(
        {str(original): (100.0, 3)}, fail_paths=frozenset({str(temp)})
    )

    with pytest.raises(FfprobeError):
        apply_remux_result(original, remux, added_track_count=1, runner=runner)  # type: ignore[arg-type]

    assert original.read_bytes() == b"ORIGINAL"
    assert not temp.exists()
    assert list(tmp_path.iterdir()) == [original]


def test_apply_raises_value_error_for_unsuccessful_remux(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    original.write_bytes(b"ORIGINAL")
    failed = RemuxResult(success=False, temp_file_path=None, returncode=1, stderr="boom")

    with pytest.raises(ValueError, match="successful RemuxResult"):
        apply_remux_result(original, failed, added_track_count=1)

    assert original.read_bytes() == b"ORIGINAL"


def test_apply_raises_value_error_for_negative_added_track_count(tmp_path: Path) -> None:
    _, _, remux = _make_files(tmp_path)
    original = tmp_path / "movie.mkv"

    with pytest.raises(ValueError, match="added_track_count"):
        apply_remux_result(original, remux, added_track_count=-1)
