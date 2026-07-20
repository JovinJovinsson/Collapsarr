"""Tests for FFprobe-based audio-stream metadata extraction (COL-15).

Two layers, mirroring ``tests/test_arr_files.py``'s split between real HTTP
fixtures and injected transports:

- Real committed media fixtures under ``tests/fixtures/downmix/`` run
  through the actual ``ffprobe`` binary, covering stereo-only, 5.1, 7.1, and
  multi-language sources per the acceptance criteria. Skipped if ffprobe
  isn't installed on the machine running the suite.
- An injected ``runner`` drives fast, environment-independent unit tests of
  error handling and malformed/edge-case ffprobe output, without needing the
  binary at all.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from collapsarr.downmix.probe import (
    AudioStreamInfo,
    FfprobeError,
    FfprobeNotFoundError,
    probe_audio_streams,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "downmix"

requires_ffprobe = pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe is not installed on this machine"
)


# ---------------------------------------------------------------------------
# Real fixture media files, probed via the actual ffprobe binary.
# ---------------------------------------------------------------------------


@requires_ffprobe
def test_probes_stereo_only_file() -> None:
    """A stereo-only file yields a single stream with language/channels/layout/codec."""
    streams = probe_audio_streams(FIXTURES_DIR / "stereo_eng.mkv")

    assert streams == [
        AudioStreamInfo(index=0, codec="aac", channels=2, channel_layout="stereo", language="eng")
    ]


@requires_ffprobe
def test_probes_5_1_file() -> None:
    """A 5.1 file reports 6 channels and a 5.1 channel layout."""
    streams = probe_audio_streams(FIXTURES_DIR / "surround_51.mkv")

    assert streams == [
        AudioStreamInfo(
            index=0, codec="ac3", channels=6, channel_layout="5.1(side)", language="eng"
        )
    ]


@requires_ffprobe
def test_probes_7_1_file() -> None:
    """A 7.1 file reports 8 channels and a 7.1 channel layout."""
    streams = probe_audio_streams(FIXTURES_DIR / "surround_71.mkv")

    assert streams == [
        AudioStreamInfo(index=0, codec="flac", channels=8, channel_layout="7.1", language="eng")
    ]


@requires_ffprobe
def test_probes_multi_language_file() -> None:
    """A file with multiple audio streams returns one entry per stream, each tagged."""
    streams = probe_audio_streams(FIXTURES_DIR / "multi_lang.mkv")

    assert streams == [
        AudioStreamInfo(index=0, codec="aac", channels=2, channel_layout="stereo", language="eng"),
        AudioStreamInfo(
            index=1, codec="ac3", channels=6, channel_layout="5.1(side)", language="fre"
        ),
    ]


@requires_ffprobe
def test_untagged_language_stream_is_bucketed_as_unknown() -> None:
    """A stream with no language tag at all is normalized to "unknown", not dropped."""
    streams = probe_audio_streams(FIXTURES_DIR / "untagged_language.mkv")

    assert streams == [
        AudioStreamInfo(
            index=0, codec="aac", channels=2, channel_layout="stereo", language="unknown"
        )
    ]


@requires_ffprobe
def test_probing_nonexistent_file_raises_ffprobe_error() -> None:
    """A path ffprobe can't open surfaces as FfprobeError (nonzero exit), not a crash."""
    with pytest.raises(FfprobeError):
        probe_audio_streams(FIXTURES_DIR / "does_not_exist.mkv")


def test_raises_ffprobe_not_found_error_for_missing_binary() -> None:
    """An unresolvable ffprobe executable raises FfprobeNotFoundError specifically."""
    with pytest.raises(FfprobeNotFoundError):
        probe_audio_streams("irrelevant.mkv", ffprobe_path="definitely-not-a-real-binary")


# ---------------------------------------------------------------------------
# Injected-runner unit tests: fast, no real subprocess/binary required.
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


def test_runner_command_selects_only_audio_streams() -> None:
    """The ffprobe invocation requests JSON output restricted to audio streams."""
    calls, runner = _stub_runner(stdout=json.dumps({"streams": []}))

    probe_audio_streams("/media/movie.mkv", runner=runner)  # type: ignore[arg-type]

    assert calls == [
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-select_streams",
            "a",
            "/media/movie.mkv",
        ]
    ]


def test_nonzero_exit_raises_ffprobe_error_with_stderr() -> None:
    """A non-zero ffprobe exit raises FfprobeError, surfacing stderr in the message."""
    _, runner = _stub_runner(returncode=1, stderr="Invalid data found when processing input")

    with pytest.raises(FfprobeError, match="Invalid data found"):
        probe_audio_streams("/media/corrupt.mkv", runner=runner)  # type: ignore[arg-type]


def test_invalid_json_output_raises_ffprobe_error() -> None:
    """Output that isn't valid JSON raises FfprobeError rather than propagating raw."""
    _, runner = _stub_runner(stdout="not json")

    with pytest.raises(FfprobeError):
        probe_audio_streams("/media/weird.mkv", runner=runner)  # type: ignore[arg-type]


def test_timeout_raises_ffprobe_error() -> None:
    """A subprocess timeout is wrapped as FfprobeError, not left as a raw TimeoutExpired."""

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

    with pytest.raises(FfprobeError):
        probe_audio_streams("/media/slow.mkv", runner=runner, timeout=5.0)  # type: ignore[arg-type]


def test_explicit_und_language_tag_is_bucketed_as_unknown() -> None:
    """An explicit ISO 639-2 "und" tag (written by some tools) also maps to "unknown"."""
    payload = {
        "streams": [
            {
                "index": 0,
                "codec_name": "dts",
                "codec_type": "audio",
                "channels": 6,
                "channel_layout": "5.1",
                "tags": {"language": "und"},
            }
        ]
    }
    _, runner = _stub_runner(stdout=json.dumps(payload))

    streams = probe_audio_streams("/media/mystery.mkv", runner=runner)  # type: ignore[arg-type]

    assert streams == [
        AudioStreamInfo(index=0, codec="dts", channels=6, channel_layout="5.1", language="unknown")
    ]


def test_missing_channel_layout_falls_back_to_channel_count() -> None:
    """A stream ffprobe can't name a layout for still gets a usable layout label."""
    payload = {
        "streams": [
            {
                "index": 0,
                "codec_name": "pcm_s16le",
                "codec_type": "audio",
                "channels": 4,
                "tags": {"language": "eng"},
            }
        ]
    }
    _, runner = _stub_runner(stdout=json.dumps(payload))

    streams = probe_audio_streams("/media/exotic.mkv", runner=runner)  # type: ignore[arg-type]

    assert streams == [
        AudioStreamInfo(
            index=0, codec="pcm_s16le", channels=4, channel_layout="4ch", language="eng"
        )
    ]


def test_non_audio_streams_are_filtered_out() -> None:
    """A video/subtitle stream mixed into the payload is ignored, not misparsed."""
    payload = {
        "streams": [
            {"index": 0, "codec_name": "h264", "codec_type": "video"},
            {
                "index": 1,
                "codec_name": "aac",
                "codec_type": "audio",
                "channels": 2,
                "channel_layout": "stereo",
                "tags": {"language": "eng"},
            },
            {"index": 2, "codec_name": "subrip", "codec_type": "subtitle"},
        ]
    }
    _, runner = _stub_runner(stdout=json.dumps(payload))

    streams = probe_audio_streams("/media/withvideo.mkv", runner=runner)  # type: ignore[arg-type]

    assert streams == [
        AudioStreamInfo(index=1, codec="aac", channels=2, channel_layout="stereo", language="eng")
    ]


def test_malformed_stream_entries_are_skipped_not_raised() -> None:
    """A stream entry missing required fields is skipped rather than raising."""
    payload = {
        "streams": [
            {"index": 0, "codec_type": "audio"},  # missing codec_name/channels
            {
                "index": 1,
                "codec_name": "aac",
                "codec_type": "audio",
                "channels": 2,
                "channel_layout": "stereo",
                "tags": {"language": "eng"},
            },
        ]
    }
    _, runner = _stub_runner(stdout=json.dumps(payload))

    streams = probe_audio_streams("/media/partial.mkv", runner=runner)  # type: ignore[arg-type]

    assert streams == [
        AudioStreamInfo(index=1, codec="aac", channels=2, channel_layout="stereo", language="eng")
    ]


def test_empty_streams_list_returns_empty_list() -> None:
    """A file with no audio streams at all returns an empty list, not an error."""
    _, runner = _stub_runner(stdout=json.dumps({"streams": []}))

    assert probe_audio_streams("/media/silent-video.mkv", runner=runner) == []  # type: ignore[arg-type]
