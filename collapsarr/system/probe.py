"""Injectable system-info probe (COL-123): Python version, OS platform, FFmpeg version.

A ``Protocol`` + default real-implementation adapter, mirroring
:mod:`collapsarr.health.disk_space`'s ``DiskUsage``/``make_disk_space_check_run``
injection idiom and :mod:`collapsarr.health.ffmpeg`'s ``checker`` idiom. Backs
the new Status page's "About" panel (``GET /api/system/info``,
:mod:`collapsarr.system.info`) with the three facts that need either a
subprocess call (FFmpeg version) or otherwise benefit from being swappable in
tests: Python version, the OS platform string, and the installed FFmpeg
version.

FFmpeg version is the one field callers should avoid probing per-request --
:func:`probe_ffmpeg_version` shells out to ``ffmpeg -version``, so
:func:`collapsarr.main.create_app`'s lifespan calls it exactly once at
startup and caches the result on ``app.state.ffmpeg_version`` (see that
module's docstring); it cannot change without a process restart. Python
version and OS platform are plain, cheap stdlib reads
(:func:`platform.python_version`/:func:`platform.platform`) with no need for
that caching -- :meth:`SystemProbe.python_version`/
:meth:`SystemProbe.os_platform` are called fresh on every
``GET /api/system/info`` request.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

DEFAULT_FFMPEG_PATH = "ffmpeg"
#: Short timeout for a version probe -- `ffmpeg -version` does no real
#: transcoding work, so this is much shorter than
#: :mod:`collapsarr.downmix.remux`'s transcode timeout. A hung `-version`
#: call would otherwise block whichever request falls back to a live probe.
FFMPEG_VERSION_TIMEOUT = 5.0

#: Injectable subprocess runner, matching the ``_Runner`` idiom
#: :mod:`collapsarr.downmix.probe`/:mod:`collapsarr.downmix.remux` already use
#: for their own ffprobe/ffmpeg invocations -- lets tests exercise every
#: branch (missing binary, timeout, non-zero exit, unparseable output)
#: without a real subprocess call.
_Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def _run_ffmpeg_version(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - command is built from fixed flags + a resolved path, not shell text
        list(command), capture_output=True, text=True, timeout=timeout, check=False
    )


class SystemProbe(Protocol):
    """Injectable source of runtime/environment facts for the About panel."""

    def python_version(self) -> str: ...  # noqa: D102 - trivial Protocol accessor

    def os_platform(self) -> str: ...  # noqa: D102 - trivial Protocol accessor

    def ffmpeg_version(self) -> str | None: ...  # noqa: D102 - trivial Protocol accessor


def _parse_ffmpeg_version(output: str) -> str | None:
    """Extract the version token from ``ffmpeg -version``'s first output line.

    The first line looks like ``"ffmpeg version 6.1.1-static Copyright (c)
    ..."`` (or ``"ffmpeg version n6.1.1 Copyright..."`` on some distro
    builds) -- the third whitespace-separated token. Returns ``None`` for
    empty/unexpected output rather than raising, matching this module's
    never-raises probe convention.
    """
    first_line = output.splitlines()[0] if output else ""
    parts = first_line.split()
    if len(parts) >= 3 and parts[0] == "ffmpeg" and parts[1] == "version":
        return parts[2]
    return None


def probe_ffmpeg_version(
    ffmpeg_path: str = DEFAULT_FFMPEG_PATH,
    timeout: float = FFMPEG_VERSION_TIMEOUT,
    runner: _Runner | None = None,
) -> str | None:
    """Return the installed FFmpeg's version string, or ``None`` if unavailable.

    Never raises -- mirrors :func:`~collapsarr.health.ffmpeg.check_ffmpeg`'s
    never-raising probe idiom: a missing binary, a timeout, a non-zero exit,
    or unparseable output all report ``None`` rather than erroring the
    caller. The FFmpeg *presence* health check (COL-38/COL-75) already
    surfaces a missing FFmpeg as its own failure state -- this probe is
    purely informational for the About panel, so it never raises on top of
    that. ``runner`` overrides the subprocess call (defaults to the real
    :func:`subprocess.run`), letting tests exercise the timeout/non-zero-exit/
    unparseable-output branches without a real binary.
    """
    resolved = shutil.which(ffmpeg_path)
    if resolved is None:
        return None
    run = runner or _run_ffmpeg_version
    try:
        result = run([resolved, "-version"], timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _parse_ffmpeg_version(result.stdout)


@dataclass(frozen=True, slots=True)
class DefaultSystemProbe:
    """The real :class:`SystemProbe`, backed by :mod:`platform`/:mod:`subprocess`."""

    ffmpeg_path: str = DEFAULT_FFMPEG_PATH

    def python_version(self) -> str:
        """Return the running interpreter's version, e.g. ``"3.12.4"``."""
        return platform.python_version()

    def os_platform(self) -> str:
        """Return a single descriptive OS/platform string (:func:`platform.platform`)."""
        return platform.platform()

    def ffmpeg_version(self) -> str | None:
        """Return the installed FFmpeg's version, or ``None`` if unavailable."""
        return probe_ffmpeg_version(self.ffmpeg_path)


__all__ = [
    "DEFAULT_FFMPEG_PATH",
    "FFMPEG_VERSION_TIMEOUT",
    "DefaultSystemProbe",
    "SystemProbe",
    "probe_ffmpeg_version",
]
