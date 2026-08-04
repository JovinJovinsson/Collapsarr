"""Docker install-method detection (COL-90).

The install-method-specific upgrade instructions the Updates page shows
(``docker pull`` + recreate-container vs. ``pipx upgrade``/``pip install
--upgrade``, per ``docs/adr/0001-update-check-detect-notify-only.md``) depend
on how this instance is actually running -- something only the *backend* can
know (the frontend has no filesystem access), so detection happens here and
is surfaced as a plain field on ``GET /api/system/updates``
(:mod:`collapsarr.update_check.routes`).

Mirrors :func:`collapsarr.health.ffmpeg.check_ffmpeg`'s shape: a pure,
never-raising presence probe with an injectable default so tests don't need
to actually be inside a container to exercise both branches.
"""

from __future__ import annotations

from pathlib import Path

#: The marker file the Docker engine creates inside every container it
#: starts (absent under bare-metal/PyPI/pipx installs, and under other
#: container runtimes -- this is deliberately Docker-specific, matching the
#: ADR's Docker-vs-pipx/bare-metal instruction split, not a general
#: "am I containerized" probe).
DOCKERENV_PATH = "/.dockerenv"


def is_docker_environment(dockerenv_path: str = DOCKERENV_PATH) -> bool:
    """Return whether this process is running inside a Docker container.

    A presence check only (:meth:`pathlib.Path.exists`) -- never raises.
    ``dockerenv_path`` defaults to :data:`DOCKERENV_PATH`; tests inject a
    path to a file they control (or a path guaranteed not to exist) instead
    of depending on the real filesystem root.
    """
    return Path(dockerenv_path).exists()


__all__ = ["DOCKERENV_PATH", "is_docker_environment"]
