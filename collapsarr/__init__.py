"""Collapsarr — an *arr-family companion app.

Detects monitored media files that are missing a lower-channel-count audio
track and adds one via FFmpeg, without ever touching the original track.

This package hosts the FastAPI backend. The public entry points are
:func:`collapsarr.main.create_app` (the application factory) and the
module-level ``app`` instance it builds.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

__all__ = ["__version__"]


def _resolve_version() -> str:
    """Return the installed package version, the single source of truth.

    Reads ``collapsarr``'s distribution metadata -- the ``version`` field
    ``pyproject.toml`` carries, which both ``.github/workflows/release.yml`` and
    ``.github/workflows/beta.yml`` ``sed``-stamp before building (a stable
    ``<base>`` or an orderable ``<base>.<build>+beta``, COL-96). Reading it here
    means CI stamps exactly one file and every runtime consumer of
    ``__version__`` (``GET /health``, the beta-channel auto-detect, the
    up-to-date comparisons) reflects the real build with no second file to keep
    in lockstep.

    Falls back to ``"0.0.0.dev0"`` only when the distribution isn't installed --
    i.e. a bare source checkout run without ``pip install -e``. The documented
    dev path (README: ``pip install -e ".[dev]"``) installs the metadata, so
    tests and ``python -m collapsarr`` resolve the real version.
    """
    try:
        return _package_version("collapsarr")
    except PackageNotFoundError:  # pragma: no cover - source checkout without install
        return "0.0.0.dev0"


__version__ = _resolve_version()
