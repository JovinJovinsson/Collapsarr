"""Unit tests for Docker install-method detection (COL-90).

Pure, I/O-only-via-``Path.exists`` unit tests for
``collapsarr.update_check.environment.is_docker_environment`` -- mirrors
``tests/test_health_check.py``'s style for ``check_ffmpeg`` (an injectable
presence probe exercised against a controlled path, never the real
filesystem root).
"""

from __future__ import annotations

from pathlib import Path

from collapsarr.update_check.environment import DOCKERENV_PATH, is_docker_environment


def test_reports_docker_when_the_marker_file_exists(tmp_path: Path) -> None:
    marker = tmp_path / ".dockerenv"
    marker.write_text("")

    assert is_docker_environment(str(marker)) is True


def test_reports_not_docker_when_the_marker_file_is_absent(tmp_path: Path) -> None:
    marker = tmp_path / ".dockerenv"

    assert is_docker_environment(str(marker)) is False


def test_default_argument_is_the_conventional_dockerenv_path() -> None:
    assert DOCKERENV_PATH == "/.dockerenv"
