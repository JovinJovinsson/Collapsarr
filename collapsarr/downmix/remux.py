"""Build and run the FFmpeg remux that adds missing downmix tracks (COL-17).

:mod:`collapsarr.downmix.probe` (COL-15) and :mod:`collapsarr.downmix.targets`
(COL-16) turn a file into, respectively, its full audio-stream inventory and
the ``(language, target)`` pairs that qualify for a new downmixed track. This
module is the next step: given those two inputs plus the file itself, build
and run the FFmpeg command that writes a temp file — in the same directory as
the original, so a later atomic rename (COL-18) stays on one filesystem —
containing every original stream (video, existing audio, subtitles)
stream-copied untouched, plus one newly *encoded* audio track per qualifying
pair, tagged with that pair's language.

For each qualifying pair the source stream to downmix from is the existing
stream for that language with the highest channel count (ties broken toward
the lowest stream index) — the best available source, consistent with
:func:`~collapsarr.downmix.targets.detect_qualifying_targets`'s no-upmix
guarantee that every qualifying target has strictly fewer channels than that.

Codec/bitrate defaults (AAC for Stereo, AC3 @ 448kbps for 2.1/5.1) come from
:class:`~collapsarr.downmix.targets.DownmixSettings`, overridable via its
``stereo_codec``/``stereo_bitrate_kbps``/``surround_codec``/
``surround_bitrate_kbps`` fields.

This runs a real subprocess against a real (large, slow) media file, so
:func:`run_remux` never raises for a *runtime* failure (missing ffmpeg
binary, non-zero exit, timeout) — it always returns a :class:`RemuxResult`
carrying ``success``, the ffmpeg exit code, and stderr, so a caller can
persist that into job history rather than crash. It *does* raise
``ValueError`` for programmer-input errors (an empty ``qualifying_targets``,
or a qualifying pair whose language has no matching stream in ``streams``) —
those indicate a caller bug (mismatched inputs), not an ffmpeg failure, and
are cheap to catch before any subprocess or temp file is involved.

On any failure path — including a caller-input ``ValueError`` raised after
the temp file was reserved — no orphaned temp file is left on disk.

Tests mirror :mod:`collapsarr.downmix.probe`: real committed fixture media
under ``tests/fixtures/downmix/`` run through the actual ``ffmpeg`` binary
(skipped when ffmpeg isn't installed), plus an injectable ``runner`` for
fast, environment-independent unit tests of command construction and error
handling.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget, QualifyingTarget

_DEFAULT_FFMPEG_PATH = "ffmpeg"
_DEFAULT_TIMEOUT = 3600.0
_TEMP_SUFFIX = ".collapsarr-tmp"

_TARGET_CHANNELS: dict[DownmixTarget, int] = {
    DownmixTarget.STEREO: 2,
    DownmixTarget.TWO_POINT_ONE: 3,
    DownmixTarget.FIVE_POINT_ONE: 6,
}

# FFmpeg's own named channel layouts (see `ffmpeg -layouts`). Using these
# explicitly (rather than relying on `-ac <n>` alone) matters most for 2.1:
# ffmpeg's *default* 3-channel layout is "3.0" (L+R+C), not "2.1" (L+R+LFE).
_TARGET_CHANNEL_LAYOUT: dict[DownmixTarget, str] = {
    DownmixTarget.STEREO: "stereo",
    DownmixTarget.TWO_POINT_ONE: "2.1",
    DownmixTarget.FIVE_POINT_ONE: "5.1",
}

_Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True, slots=True)
class RemuxResult:
    """Outcome of a :func:`run_remux` attempt, shaped for job-history logging.

    ``temp_file_path`` is populated only when ``success`` is ``True`` — on
    any failure the temp file (if one was created at all) is deleted before
    returning, so callers never observe an orphaned file. ``returncode`` and
    ``stderr`` are always populated: ffmpeg's own values on a completed
    subprocess run, or a synthesized ``returncode=-1`` with a descriptive
    ``stderr`` for pre-flight errors (missing binary, timeout) that never
    produced a real exit code.
    """

    success: bool
    temp_file_path: Path | None
    returncode: int
    stderr: str


def build_remux_command(
    file_path: str | Path,
    temp_file_path: str | Path,
    streams: Sequence[AudioStreamInfo],
    qualifying_targets: Sequence[QualifyingTarget],
    settings: DownmixSettings,
    *,
    ffmpeg_path: str = _DEFAULT_FFMPEG_PATH,
    default_audio_index: int | None = None,
) -> list[str]:
    """Build the FFmpeg argv that remuxes ``file_path`` into ``temp_file_path``.

    ``-map 0`` copies every original stream (video, all existing audio,
    subtitles, ...) in place, stream-copied via a blanket ``-c copy``. One
    additional ``-map 0:<index>`` is appended per entry in
    ``qualifying_targets``, each re-encoding its resolved source stream
    (see module docstring) down to the target's channel count/layout via
    per-output-stream ``-c:a:a:N``/``-ac:a:N``/``-channel_layout:a:N``
    overrides (the ``a:N`` form — audio-type-relative index — rather than a
    bare ``N``, which ffmpeg instead resolves as the *absolute* output
    stream index and would silently target the wrong stream once a video or
    subtitle stream is present), with the codec/bitrate drawn from
    ``settings`` and an ``-metadata:s:a:N language=<lang>`` tag.

    ``default_audio_index`` folds the automatic Default Audio Track fix
    (COL-152) into this *same* invocation: when it is ``None`` (the default),
    **no** ``-disposition`` flag is emitted at all, so the command is
    byte-for-byte what it was before the feature existed. When it is an output
    audio-relative index, that output audio stream gets
    ``-disposition:a:N default`` and every *other* output audio stream (existing
    copies and newly-encoded tracks alike) gets ``-disposition:a:N 0`` —
    explicit on all of them, so the winner is set and any wrongly-defaulted
    track is cleared regardless of what disposition ``-c copy`` would otherwise
    carry over. The output audio layout is the existing streams in order
    (indices ``0 .. len(streams)-1``) followed by one per qualifying target;
    the caller resolves ``default_audio_index`` against that layout via
    :func:`~collapsarr.downmix.default_audio.resolve_default_audio_output_index`.

    Raises:
        ValueError: ``qualifying_targets`` is empty *and* ``default_audio_index``
            is ``None`` — with no new tracks to add and no disposition change
            to make, there is nothing at all for this remux to do (this is the
            one case ``qualifying_targets`` may legitimately be empty: a
            disposition-only remux — see
            :func:`~collapsarr.downmix.default_audio_pipeline.run_default_audio_pipeline`
            — passes an empty sequence with a real ``default_audio_index``, and
            that combination is deliberately allowed through); or one of
            ``qualifying_targets``'s entries has a language with no matching
            stream in ``streams`` — mismatched inputs (this file's own
            probe/detect output should never produce this), not an ffmpeg
            runtime failure; or ``default_audio_index`` falls outside the
            output audio layout.
    """
    if not qualifying_targets and default_audio_index is None:
        raise ValueError(
            "qualifying_targets must not be empty when default_audio_index is "
            "None — nothing to remux"
        )
    sources = _resolve_sources(streams, qualifying_targets)
    total_audio_streams = len(streams) + len(sources)
    if default_audio_index is not None and not 0 <= default_audio_index < total_audio_streams:
        raise ValueError(
            f"default_audio_index {default_audio_index} is out of range for "
            f"{total_audio_streams} output audio stream(s)"
        )

    command = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(file_path),
        "-map",
        "0",
    ]
    for _target, source in sources:
        command += ["-map", f"0:{source.index}"]

    command += ["-c", "copy"]

    new_audio_start = len(streams)
    for position, (target, _source) in enumerate(sources):
        audio_index = new_audio_start + position
        codec, bitrate_kbps = _codec_settings_for(target.target, settings)
        channels = _TARGET_CHANNELS[target.target]
        layout = _TARGET_CHANNEL_LAYOUT[target.target]
        command += [f"-c:a:{audio_index}", codec]
        command += [f"-ac:a:{audio_index}", str(channels)]
        command += [f"-channel_layout:a:{audio_index}", layout]
        if bitrate_kbps is not None:
            command += [f"-b:a:{audio_index}", f"{bitrate_kbps}k"]
        command += [f"-metadata:s:a:{audio_index}", f"language={target.language}"]

    if default_audio_index is not None:
        for audio_index in range(total_audio_streams):
            flag = "default" if audio_index == default_audio_index else "0"
            command += [f"-disposition:a:{audio_index}", flag]

    command.append(str(temp_file_path))
    return command


def run_remux(
    file_path: str | Path,
    streams: Sequence[AudioStreamInfo],
    qualifying_targets: Sequence[QualifyingTarget],
    settings: DownmixSettings,
    *,
    ffmpeg_path: str = _DEFAULT_FFMPEG_PATH,
    timeout: float = _DEFAULT_TIMEOUT,
    default_audio_index: int | None = None,
    runner: _Runner | None = None,
) -> RemuxResult:
    """Run the FFmpeg remux for ``file_path``, returning a structured result.

    Writes the temp file into ``file_path``'s own parent directory (same
    filesystem, required for a later atomic rename in COL-18), reserving a
    unique name via :func:`tempfile.mkstemp` before invoking ffmpeg so two
    concurrent jobs on the same file can never collide.

    ``default_audio_index`` is forwarded to :func:`build_remux_command` (see
    there): ``None`` emits no disposition flags, keeping the command
    byte-for-byte unchanged; an output audio-relative index folds the Default
    Audio Track fix into this same invocation and atomic swap.

    ``runner`` overrides how the subprocess is invoked (signature
    ``(command, timeout) -> subprocess.CompletedProcess``); it defaults to a
    thin wrapper around :func:`subprocess.run` and exists so tests can drive
    this without the ``ffmpeg`` binary installed, mirroring
    :func:`collapsarr.downmix.probe.probe_audio_streams`.

    Never raises for a runtime ffmpeg failure (missing binary, non-zero
    exit, timeout) — those come back as ``RemuxResult(success=False, ...)``
    with the exit code and stderr populated for job-history logging, and any
    temp file created along the way is removed first. Does raise
    ``ValueError`` for the caller-input errors documented on
    :func:`build_remux_command`, raised before any temp file is created.
    """
    path = Path(file_path)
    # Validate before touching the filesystem: a bad-input ValueError here
    # (empty/mismatched targets, or an out-of-range default_audio_index) must
    # never leave a temp file behind. Building the command against a throwaway
    # path exercises exactly the same validation build_remux_command applies.
    build_remux_command(
        path,
        path,
        streams,
        qualifying_targets,
        settings,
        ffmpeg_path=ffmpeg_path,
        default_audio_index=default_audio_index,
    )

    run = runner or _run_ffmpeg
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}.", suffix=f"{_TEMP_SUFFIX}{path.suffix}"
    )
    os.close(fd)
    temp_path = Path(temp_name)

    command = build_remux_command(
        path,
        temp_path,
        streams,
        qualifying_targets,
        settings,
        ffmpeg_path=ffmpeg_path,
        default_audio_index=default_audio_index,
    )

    try:
        result = run(command, timeout)
    except FileNotFoundError:
        _remove_if_exists(temp_path)
        return RemuxResult(
            success=False,
            temp_file_path=None,
            returncode=-1,
            stderr=f"ffmpeg executable not found: {ffmpeg_path!r}",
        )
    except subprocess.TimeoutExpired:
        _remove_if_exists(temp_path)
        return RemuxResult(
            success=False,
            temp_file_path=None,
            returncode=-1,
            stderr=f"ffmpeg timed out after {timeout}s remuxing {str(path)!r}",
        )

    if result.returncode != 0:
        _remove_if_exists(temp_path)
        return RemuxResult(
            success=False,
            temp_file_path=None,
            returncode=result.returncode,
            stderr=result.stderr,
        )

    return RemuxResult(
        success=True, temp_file_path=temp_path, returncode=0, stderr=result.stderr
    )


def _run_ffmpeg(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - command is built from fixed flags + paths, not shell text
        list(command), capture_output=True, text=True, timeout=timeout, check=False
    )


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _codec_settings_for(
    target: DownmixTarget, settings: DownmixSettings
) -> tuple[str, int | None]:
    if target is DownmixTarget.STEREO:
        return settings.stereo_codec, settings.stereo_bitrate_kbps
    return settings.surround_codec, settings.surround_bitrate_kbps


def _resolve_sources(
    streams: Sequence[AudioStreamInfo], qualifying_targets: Sequence[QualifyingTarget]
) -> list[tuple[QualifyingTarget, AudioStreamInfo]]:
    """Pair each qualifying target with the stream to downmix it from.

    The source for a given language is its highest-channel-count existing
    stream (ties broken toward the lowest stream index), matching
    :func:`~collapsarr.downmix.targets.detect_qualifying_targets`'s
    guarantee that every qualifying target has fewer channels than that.

    An empty ``qualifying_targets`` is valid here — it simply resolves to no
    sources — the "empty is meaningless" guard lives in
    :func:`build_remux_command`, which also knows about ``default_audio_index``
    and so can tell a genuinely-empty request apart from a disposition-only one.
    """
    resolved: list[tuple[QualifyingTarget, AudioStreamInfo]] = []
    for target in qualifying_targets:
        candidates = [s for s in streams if s.language == target.language]
        if not candidates:
            raise ValueError(
                f"no source audio stream found for language {target.language!r} "
                f"(target {target.target.value!r})"
            )
        source = max(candidates, key=lambda s: (s.channels, -s.index))
        resolved.append((target, source))

    return resolved
