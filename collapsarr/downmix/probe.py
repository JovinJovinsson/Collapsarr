"""Extract per-stream audio metadata from a media file via FFprobe (COL-15).

:mod:`collapsarr.arr.files` normalizes the *aggregate* audio fields Sonarr/Radarr
report in their ``mediaInfo`` block (total channel count, a single codec,
etc.) and explicitly defers per-stream detail: "A per-stream probe (e.g. via
ffprobe) is out of scope here." This module is that probe. Given a resolved
local file path, it shells out to ``ffprobe`` and returns one
:class:`AudioStreamInfo` per audio stream in the file, normalized to
language tag, channel count, channel layout, and codec — the input shape the
target-detection logic (COL-16) consumes to decide which downmix targets, if
any, a file qualifies for.

Untagged or unrecognized-language streams are bucketed as ``"unknown"``
rather than dropped or raising, matching real-world files (many rips carry
no language tag at all, and MKV's own convention is to omit the tag rather
than write the ISO 639-2 "und" placeholder — both cases are treated the
same way here).

Tests exercise this module two ways: real committed fixture media files
under ``tests/fixtures/downmix/`` run through the real ``ffprobe`` binary
(skipped when ffprobe isn't installed), and an injectable ``runner`` for
fast, environment-independent unit tests of error handling and malformed
ffprobe output — mirroring how :mod:`collapsarr.arr.files` injects an
``httpx`` transport.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_FFPROBE_PATH = "ffprobe"
_DEFAULT_TIMEOUT = 30.0
_UNKNOWN_LANGUAGE = "unknown"
# ISO 639-2 "undetermined" — the placeholder some tools write explicitly;
# others (including ffmpeg's own Matroska muxer) simply omit the tag. Both
# mean the same thing to us: no usable language tag.
_UNDETERMINED_LANGUAGE_CODES = {"und"}

_Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


class FfprobeError(RuntimeError):
    """Raised when ffprobe fails to run, exits non-zero, or emits unparseable output."""


class FfprobeNotFoundError(FfprobeError):
    """Raised when the configured ffprobe executable cannot be found."""


@dataclass(frozen=True, slots=True)
class AudioStreamInfo:
    """Normalized metadata for a single audio stream, as reported by ffprobe.

    ``language`` is always a lowercase tag, normalized to ``"unknown"`` when
    the stream has no language tag (or an explicit ISO 639-2 "und") rather
    than being omitted or raising. ``channel_layout`` falls back to
    ``"<channels>ch"`` on the rare stream ffprobe can't name a layout for, so
    it is always populated too.
    """

    index: int
    codec: str
    channels: int
    channel_layout: str
    language: str


def probe_audio_streams(
    file_path: str | Path,
    *,
    ffprobe_path: str = _DEFAULT_FFPROBE_PATH,
    timeout: float = _DEFAULT_TIMEOUT,
    runner: _Runner | None = None,
) -> list[AudioStreamInfo]:
    """Return the normalized audio streams of ``file_path``, probed via ffprobe.

    Runs ``ffprobe -show_streams -select_streams a`` against ``file_path``
    and parses its JSON output. ``runner`` overrides how the subprocess is
    invoked (signature ``(command, timeout) -> subprocess.CompletedProcess``);
    it defaults to a thin wrapper around :func:`subprocess.run` and exists so
    tests can supply canned ffprobe output without needing the binary
    installed.

    Raises:
        FfprobeNotFoundError: ``ffprobe_path`` could not be found/executed.
        FfprobeError: ffprobe timed out, exited non-zero, or produced output
            that isn't the JSON shape ``-show_streams`` is expected to emit.
    """
    command = [
        ffprobe_path,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-select_streams",
        "a",
        str(file_path),
    ]
    run = runner or _run_ffprobe

    try:
        result = run(command, timeout)
    except FileNotFoundError as exc:
        raise FfprobeNotFoundError(
            f"ffprobe executable not found: {ffprobe_path!r}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FfprobeError(
            f"ffprobe timed out after {timeout}s probing {str(file_path)!r}"
        ) from exc

    if result.returncode != 0:
        raise FfprobeError(
            f"ffprobe exited with status {result.returncode} probing "
            f"{str(file_path)!r}: {result.stderr.strip()}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FfprobeError(
            f"ffprobe returned output that could not be parsed as JSON "
            f"probing {str(file_path)!r}"
        ) from exc

    return _parse_audio_streams(payload)


def _run_ffprobe(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - command is built from fixed flags + a path, not shell text
        list(command), capture_output=True, text=True, timeout=timeout, check=False
    )


def _parse_audio_streams(payload: object) -> list[AudioStreamInfo]:
    """Normalize an ffprobe ``-show_streams`` JSON payload into a stream list.

    Defensive against malformed/unexpected shapes the same way
    :func:`collapsarr.arr.files._extract_audio_info` is: anything not
    matching the expected type is skipped rather than raising, since a
    partially-readable file is more useful to callers than a hard failure.
    """
    if not isinstance(payload, dict):
        return []

    streams = payload.get("streams")
    if not isinstance(streams, list):
        return []

    results: list[AudioStreamInfo] = []
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
            continue

        index = stream.get("index")
        codec = stream.get("codec_name")
        channels = stream.get("channels")
        if not isinstance(index, int) or not isinstance(codec, str) or not isinstance(
            channels, int
        ):
            continue

        results.append(
            AudioStreamInfo(
                index=index,
                codec=codec,
                channels=channels,
                channel_layout=_normalize_channel_layout(stream.get("channel_layout"), channels),
                language=_normalize_language(stream.get("tags")),
            )
        )

    return results


def _normalize_channel_layout(raw_layout: object, channels: int) -> str:
    if isinstance(raw_layout, str) and raw_layout and raw_layout != "unknown":
        return raw_layout
    return f"{channels}ch"


def _normalize_language(tags: object) -> str:
    if isinstance(tags, dict):
        raw_language = tags.get("language")
        if isinstance(raw_language, str) and raw_language.strip():
            language = raw_language.strip().lower()
            if language not in _UNDETERMINED_LANGUAGE_CODES:
                return language
    return _UNKNOWN_LANGUAGE
