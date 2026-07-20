"""Collapsarr — an *arr-family companion app.

Detects monitored media files that are missing a lower-channel-count audio
track and adds one via FFmpeg, without ever touching the original track.

This package hosts the FastAPI backend. The public entry points are
:func:`collapsarr.main.create_app` (the application factory) and the
module-level ``app`` instance it builds.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
