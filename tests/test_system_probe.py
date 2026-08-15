"""Unit tests for the injectable system-info probe (COL-123).

``DefaultSystemProbe.python_version``/``os_platform`` are plain,
never-failing stdlib reads with nothing branchy to test beyond "returns a
non-empty string" -- these tests focus on :func:`probe_ffmpeg_version`, which
has to handle a missing binary, a timeout, a non-zero exit, and unparseable
output without raising, matching :func:`~collapsarr.health.ffmpeg.
check_ffmpeg`'s never-raises convention. A fake ``runner`` (matching
:mod:`collapsarr.downmix.probe`/:mod:`collapsarr.downmix.remux`'s own
``_Runner`` injection idiom) exercises each subprocess-outcome branch, and
:func:`shutil.which` is monkeypatched to a fixed resolved path so every test
here is deterministic and independent of whether ``ffmpeg`` is actually
installed on the machine running the suite.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from collapsarr.system.probe import (
    DefaultSystemProbe,
    _parse_ffmpeg_version,
    probe_ffmpeg_version,
)


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture(autouse=True)
def _resolved_ffmpeg_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``shutil.which("ffmpeg")`` resolve deterministically for every test.

    Every ``probe_ffmpeg_version`` test below wants to reach its injected
    ``runner`` (or, for the "not on PATH" test, explicitly asks for a bogus
    path that ``shutil.which`` genuinely won't find) -- patching this at the
    module level means these tests don't depend on whether ``ffmpeg`` is
    actually installed on the machine running the suite.
    """
    monkeypatch.setattr("collapsarr.system.probe.shutil.which", lambda _name: "/usr/bin/ffmpeg")


# --- _parse_ffmpeg_version (pure) -------------------------------------------


def test_parses_the_version_token_from_a_typical_first_line() -> None:
    output = (
        "ffmpeg version 6.1.1-static Copyright (c) 2000-2024 the FFmpeg developers\nbuilt with ..."
    )
    assert _parse_ffmpeg_version(output) == "6.1.1-static"


def test_parses_a_distro_style_version_token() -> None:
    output = "ffmpeg version n6.1.1 Copyright (c) 2000-2024 the FFmpeg developers"
    assert _parse_ffmpeg_version(output) == "n6.1.1"


def test_returns_none_for_empty_output() -> None:
    assert _parse_ffmpeg_version("") is None


def test_returns_none_for_unexpected_output() -> None:
    assert _parse_ffmpeg_version("not ffmpeg output at all") is None


# --- probe_ffmpeg_version ----------------------------------------------------


def test_returns_none_when_ffmpeg_is_not_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("collapsarr.system.probe.shutil.which", lambda _name: None)
    assert probe_ffmpeg_version() is None


def test_returns_the_parsed_version_via_an_injected_runner() -> None:
    def runner(_command: Sequence[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return _completed("ffmpeg version 8.1.2 Copyright (c) 2000-2026 the FFmpeg developers\n")

    assert probe_ffmpeg_version(runner=runner) == "8.1.2"


def test_passes_the_resolved_path_and_version_flag_to_the_runner() -> None:
    seen: list[Sequence[str]] = []

    def runner(command: Sequence[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return _completed("ffmpeg version 8.1.2 Copyright (c) 2000-2026 the FFmpeg developers\n")

    probe_ffmpeg_version(runner=runner)

    assert seen == [["/usr/bin/ffmpeg", "-version"]]


def test_returns_none_on_a_non_zero_exit() -> None:
    def runner(_command: Sequence[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return _completed("", returncode=1)

    assert probe_ffmpeg_version(runner=runner) is None


def test_returns_none_on_a_timeout() -> None:
    def runner(_command: Sequence[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5.0)

    assert probe_ffmpeg_version(runner=runner) is None


def test_returns_none_on_a_missing_binary_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """``shutil.which`` found it, but it vanished before exec (rare race)."""

    def runner(_command: Sequence[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("ffmpeg")

    assert probe_ffmpeg_version(runner=runner) is None


def test_returns_none_on_unparseable_output() -> None:
    def runner(_command: Sequence[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return _completed("garbage\n")

    assert probe_ffmpeg_version(runner=runner) is None


# --- DefaultSystemProbe ------------------------------------------------------


def test_default_probe_python_version_is_a_non_empty_string() -> None:
    assert DefaultSystemProbe().python_version()


def test_default_probe_os_platform_is_a_non_empty_string() -> None:
    assert DefaultSystemProbe().os_platform()


def test_default_probe_ffmpeg_version_never_raises_for_a_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("collapsarr.system.probe.shutil.which", lambda _name: None)
    assert DefaultSystemProbe().ffmpeg_version() is None
