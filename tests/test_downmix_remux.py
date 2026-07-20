"""Tests for the FFmpeg remux that adds missing downmix tracks (COL-17).

Three layers, mirroring ``tests/test_downmix_probe.py``'s split:

- Real committed fixture media under ``tests/fixtures/downmix/`` run through
  the actual ``ffmpeg`` binary end-to-end (build command -> run -> probe the
  result), covering the full pipeline from COL-15/16 output. Skipped if
  ffmpeg isn't installed.
- ``build_remux_command`` unit tests: pure, no subprocess, assert the exact
  argv produced for various stream/target/settings combinations.
- ``run_remux`` unit tests with an injected ``runner``: fast,
  environment-independent coverage of the temp-file lifecycle and error
  handling (missing binary, non-zero exit, timeout).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from collapsarr.downmix.probe import AudioStreamInfo, probe_audio_streams
from collapsarr.downmix.remux import build_remux_command, run_remux
from collapsarr.downmix.targets import (
    DownmixSettings,
    DownmixTarget,
    QualifyingTarget,
    detect_qualifying_targets,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "downmix"

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed on this machine"
)

ALL_TARGETS = frozenset(
    {DownmixTarget.STEREO, DownmixTarget.TWO_POINT_ONE, DownmixTarget.FIVE_POINT_ONE}
)


def _stream(
    *, index: int = 0, channels: int, language: str = "eng", codec: str = "flac"
) -> AudioStreamInfo:
    return AudioStreamInfo(
        index=index,
        codec=codec,
        channels=channels,
        channel_layout=f"{channels}ch",
        language=language,
    )


def _probe_stream_summary(path: Path) -> list[tuple[str, str, int]]:
    """Return ``(codec_type, codec_name, channels-or-0)`` for every stream, in order."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    return [
        (s["codec_type"], s["codec_name"], s.get("channels", 0)) for s in payload["streams"]
    ]


# ---------------------------------------------------------------------------
# Real fixture media, run through the actual ffmpeg binary end-to-end.
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_remux_adds_stacked_stereo_and_2_1_tracks_for_the_higher_channel_language() -> None:
    """multi_lang.mkv (eng stereo, fre 5.1) qualifies fre for Stereo + 2.1; both get added."""
    fixture = FIXTURES_DIR / "multi_lang.mkv"
    streams = probe_audio_streams(fixture)
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)
    targets = detect_qualifying_targets(streams, settings)

    result = run_remux(fixture, streams, targets, settings)
    try:
        assert result.success is True
        assert result.returncode == 0
        assert result.temp_file_path is not None
        assert result.temp_file_path.parent == fixture.parent

        summary = _probe_stream_summary(result.temp_file_path)
        assert summary == [
            ("audio", "aac", 2),  # original eng stereo, copied
            ("audio", "ac3", 6),  # original fre 5.1, copied
            ("audio", "aac", 2),  # new fre Stereo track, encoded
            ("audio", "ac3", 3),  # new fre 2.1 track, encoded
        ]
    finally:
        if result.temp_file_path is not None:
            result.temp_file_path.unlink(missing_ok=True)


@requires_ffmpeg
def test_remux_preserves_video_and_subtitles_uncopied_and_tags_new_audio_language() -> None:
    """video_audio_subs.mkv's video/subtitle streams pass through untouched."""
    fixture = FIXTURES_DIR / "video_audio_subs.mkv"
    streams = probe_audio_streams(fixture)
    settings = DownmixSettings()  # Stereo only, the product default
    targets = detect_qualifying_targets(streams, settings)
    assert targets == [QualifyingTarget(language="eng", target=DownmixTarget.STEREO)]

    result = run_remux(fixture, streams, targets, settings)
    try:
        assert result.success is True
        assert result.temp_file_path is not None

        summary = _probe_stream_summary(result.temp_file_path)
        assert summary == [
            ("video", "h264", 0),
            ("audio", "ac3", 6),  # original 5.1, copied
            ("subtitle", "subrip", 0),
            ("audio", "aac", 2),  # new Stereo track, encoded
        ]

        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-select_streams",
                "a",
                str(result.temp_file_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        audio_streams = json.loads(proc.stdout)["streams"]
        assert audio_streams[1]["tags"]["language"] == "eng"
    finally:
        if result.temp_file_path is not None:
            result.temp_file_path.unlink(missing_ok=True)


@requires_ffmpeg
def test_remux_leaves_no_orphan_temp_file_when_ffmpeg_fails() -> None:
    """A real ffmpeg failure (nonexistent input) leaves no temp file behind."""
    missing = FIXTURES_DIR / "does_not_exist.mkv"
    streams = [_stream(index=0, channels=2, language="eng")]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.STEREO)]
    before = set(FIXTURES_DIR.iterdir())

    result = run_remux(missing, streams, targets, DownmixSettings())

    assert result.success is False
    assert result.temp_file_path is None
    assert result.returncode != 0
    assert result.stderr  # ffmpeg's real stderr is surfaced
    assert set(FIXTURES_DIR.iterdir()) == before


# ---------------------------------------------------------------------------
# build_remux_command: pure argv construction, no subprocess involved.
# ---------------------------------------------------------------------------


def test_build_remux_command_maps_all_original_streams_and_one_new_track() -> None:
    streams = [
        _stream(index=0, channels=2, language="eng", codec="aac"),
        _stream(index=1, channels=6, language="fre", codec="ac3"),
    ]
    targets = [QualifyingTarget(language="fre", target=DownmixTarget.STEREO)]

    command = build_remux_command(
        "/media/movie.mkv", "/media/.movie.tmp.mkv", streams, targets, DownmixSettings()
    )

    assert command == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        "/media/movie.mkv",
        "-map",
        "0",
        "-map",
        "0:1",
        "-c",
        "copy",
        "-c:a:2",
        "aac",
        "-ac:a:2",
        "2",
        "-channel_layout:a:2",
        "stereo",
        "-metadata:s:a:2",
        "language=fre",
        "/media/.movie.tmp.mkv",
    ]


def test_build_remux_command_uses_ac3_and_448k_default_for_surround_targets() -> None:
    streams = [_stream(index=0, channels=8, language="eng", codec="flac")]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.FIVE_POINT_ONE)]

    command = build_remux_command(
        "in.mkv", "out.mkv", streams, targets, DownmixSettings(enabled_targets=ALL_TARGETS)
    )

    assert "-c:a:1" in command
    assert command[command.index("-c:a:1") + 1] == "ac3"
    assert command[command.index("-ac:a:1") + 1] == "6"
    assert command[command.index("-channel_layout:a:1") + 1] == "5.1"
    assert command[command.index("-b:a:1") + 1] == "448k"


def test_build_remux_command_respects_codec_and_bitrate_overrides() -> None:
    streams = [_stream(index=0, channels=8, language="eng", codec="flac")]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.STEREO)]
    settings = DownmixSettings(stereo_codec="libfdk_aac", stereo_bitrate_kbps=192)

    command = build_remux_command("in.mkv", "out.mkv", streams, targets, settings)

    assert command[command.index("-c:a:1") + 1] == "libfdk_aac"
    assert command[command.index("-b:a:1") + 1] == "192k"


def test_build_remux_command_picks_highest_channel_source_for_a_shared_language() -> None:
    """When a language has multiple streams, the highest-channel one is the downmix source."""
    streams = [
        _stream(index=0, channels=2, language="eng", codec="aac"),
        _stream(index=2, channels=6, language="eng", codec="ac3"),
    ]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.STEREO)]

    command = build_remux_command("in.mkv", "out.mkv", streams, targets, DownmixSettings())

    # The new track is sourced from input stream 2 (6ch), not stream 0 (2ch).
    map_indices = [command[i + 1] for i, arg in enumerate(command) if arg == "-map"]
    assert map_indices == ["0", "0:2"]


def test_build_remux_command_stacks_multiple_targets_at_sequential_output_indices() -> None:
    streams = [_stream(index=0, channels=6, language="fre", codec="ac3")]
    targets = [
        QualifyingTarget(language="fre", target=DownmixTarget.STEREO),
        QualifyingTarget(language="fre", target=DownmixTarget.TWO_POINT_ONE),
    ]

    command = build_remux_command(
        "in.mkv", "out.mkv", streams, targets, DownmixSettings(enabled_targets=ALL_TARGETS)
    )

    # 1 original audio stream -> new tracks land at output audio index 1, 2.
    assert command[command.index("-c:a:1") + 1] == "aac"
    assert command[command.index("-c:a:2") + 1] == "ac3"
    map_indices = [command[i + 1] for i, arg in enumerate(command) if arg == "-map"]
    assert map_indices == ["0", "0:0", "0:0"]


def test_build_remux_command_raises_for_empty_qualifying_targets() -> None:
    streams = [_stream(index=0, channels=6, language="eng")]

    with pytest.raises(ValueError, match="qualifying_targets"):
        build_remux_command("in.mkv", "out.mkv", streams, [], DownmixSettings())


def test_build_remux_command_raises_when_no_stream_matches_targets_language() -> None:
    streams = [_stream(index=0, channels=6, language="eng")]
    targets = [QualifyingTarget(language="fre", target=DownmixTarget.STEREO)]

    with pytest.raises(ValueError, match="fre"):
        build_remux_command("in.mkv", "out.mkv", streams, targets, DownmixSettings())


# ---------------------------------------------------------------------------
# run_remux: temp-file lifecycle and error handling, via an injected runner.
# ---------------------------------------------------------------------------


def _stub_runner(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> tuple[list[list[str]], object]:
    calls: list[list[str]] = []

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(
            args=list(command), returncode=returncode, stdout=stdout, stderr=stderr
        )

    return calls, runner


def test_run_remux_writes_temp_file_in_same_directory_on_success(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"")
    streams = [_stream(index=0, channels=6, language="eng")]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.STEREO)]
    _, runner = _stub_runner(returncode=0)

    result = run_remux(source, streams, targets, DownmixSettings(), runner=runner)  # type: ignore[arg-type]

    assert result.success is True
    assert result.returncode == 0
    assert result.temp_file_path is not None
    assert result.temp_file_path.parent == tmp_path
    assert result.temp_file_path.exists()
    assert result.temp_file_path.suffix == ".mkv"
    assert result.temp_file_path.name != source.name


def test_run_remux_removes_temp_file_and_reports_failure_on_nonzero_exit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"")
    streams = [_stream(index=0, channels=6, language="eng")]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.STEREO)]
    _, runner = _stub_runner(returncode=1, stderr="Invalid data found when processing input")

    result = run_remux(source, streams, targets, DownmixSettings(), runner=runner)  # type: ignore[arg-type]

    assert result.success is False
    assert result.temp_file_path is None
    assert result.returncode == 1
    assert result.stderr == "Invalid data found when processing input"
    assert list(tmp_path.iterdir()) == [source]  # no orphaned temp file


def test_run_remux_reports_failure_for_missing_ffmpeg_binary(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"")
    streams = [_stream(index=0, channels=6, language="eng")]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.STEREO)]

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    result = run_remux(
        source,
        streams,
        targets,
        DownmixSettings(),
        ffmpeg_path="definitely-not-a-real-binary",
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert result.temp_file_path is None
    assert result.returncode == -1
    assert "definitely-not-a-real-binary" in result.stderr
    assert list(tmp_path.iterdir()) == [source]


def test_run_remux_reports_failure_on_timeout(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"")
    streams = [_stream(index=0, channels=6, language="eng")]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.STEREO)]

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

    result = run_remux(
        source, streams, targets, DownmixSettings(), timeout=5.0, runner=runner  # type: ignore[arg-type]
    )

    assert result.success is False
    assert result.temp_file_path is None
    assert result.returncode == -1
    assert "timed out" in result.stderr
    assert list(tmp_path.iterdir()) == [source]


def test_run_remux_raises_value_error_without_creating_temp_file_for_empty_targets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"")

    with pytest.raises(ValueError):
        run_remux(source, [], [], DownmixSettings())

    assert list(tmp_path.iterdir()) == [source]


def test_run_remux_raises_value_error_without_creating_temp_file_for_unmatched_language(
    tmp_path: Path,
) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"")
    streams = [_stream(index=0, channels=6, language="eng")]
    targets = [QualifyingTarget(language="fre", target=DownmixTarget.STEREO)]

    with pytest.raises(ValueError):
        run_remux(source, streams, targets, DownmixSettings())

    assert list(tmp_path.iterdir()) == [source]


def test_run_remux_passes_ffmpeg_path_and_timeout_through_to_runner(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"")
    streams = [_stream(index=0, channels=6, language="eng")]
    targets = [QualifyingTarget(language="eng", target=DownmixTarget.STEREO)]
    captured: dict[str, object] = {}

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        captured["command"] = list(command)
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="", stderr="")

    run_remux(
        source,
        streams,
        targets,
        DownmixSettings(),
        ffmpeg_path="/opt/homebrew/bin/ffmpeg",
        timeout=42.0,
        runner=runner,  # type: ignore[arg-type]
    )

    assert captured["timeout"] == 42.0
    command = captured["command"]
    assert isinstance(command, list)
    assert command[0] == "/opt/homebrew/bin/ffmpeg"
