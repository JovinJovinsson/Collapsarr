"""Restore marker: the on-disk hand-off from a staged restore to the boot swap (COL-70).

A *restore marker* is the one durable signal that a restore is pending. A
restore-request endpoint (a later slice, COL-71) stages the chosen backup's
database to a file and writes this marker pointing at it; the app then restarts
and the boot-time swap engine (:mod:`collapsarr.restore.engine`) consumes the
marker before the database engine connects.

The marker is a tiny JSON document under ``<data_dir>/restore.json`` holding the
absolute path of the staged database file. It is intentionally minimal and
schema-light so later slices (the version-compatibility guard, upload restores)
can add fields without reshaping the boot consumer, which reads only what it
needs and ignores the rest.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from collapsarr.config import Settings

logger = logging.getLogger(__name__)

#: Filename of the restore marker under ``<data_dir>``.
RESTORE_MARKER_FILENAME = "restore.json"


@dataclass(frozen=True, slots=True)
class RestoreMarker:
    """A parsed, valid restore marker.

    ``staged_path`` is the absolute path of the staged SQLite database the boot
    swap should apply. Presence of a marker means "a restore is pending"; the
    boot engine still validates the staged file before swapping it in.
    """

    staged_path: Path


def restore_marker_path(settings: Settings) -> Path:
    """Return the marker's path (``<data_dir>/restore.json``); not created here."""
    return Path(settings.data_dir).expanduser() / RESTORE_MARKER_FILENAME


def write_restore_marker(settings: Settings, staged_path: Path | str) -> RestoreMarker:
    """Write a restore marker pointing at ``staged_path`` and return it.

    The write is atomic (a dot-prefixed temp file is :func:`os.replace`-d onto
    the final name) so a crash mid-write can never leave a half-written marker
    for the next boot to trip over. ``staged_path`` is stored as an absolute
    path so it resolves the same regardless of the process working directory at
    the next boot.

    This is the seam the restore-request endpoints (COL-71) use to arm a
    restart-to-apply restore.
    """
    marker_path = restore_marker_path(settings)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(staged_path).expanduser().resolve()
    payload = {"staged_path": str(staged)}

    tmp_path = marker_path.with_name(f".{marker_path.name}.part")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp_path, marker_path)
    logger.info("Wrote restore marker %s -> %s", marker_path, staged)
    return RestoreMarker(staged_path=staged)


def read_restore_marker(settings: Settings) -> RestoreMarker | None:
    """Read and parse the restore marker, or ``None`` if it is absent or malformed.

    ``None`` for an *absent* marker is the clean "no restore pending" signal.
    A *present but malformed* marker (unreadable, not JSON, or missing/empty
    ``staged_path``) also returns ``None`` but is logged loudly first -- the
    boot engine treats it as a defensive abort and clears it, so a corrupt
    marker can never brick or loop startup.
    """
    marker_path = restore_marker_path(settings)
    if not marker_path.is_file():
        return None
    try:
        raw = json.loads(marker_path.read_text(encoding="utf-8"))
        staged = raw["staged_path"]
        if not isinstance(staged, str) or not staged:
            raise ValueError("restore marker 'staged_path' is missing or empty")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        logger.error("Restore marker at %s is malformed and will be ignored: %s", marker_path, exc)
        return None
    return RestoreMarker(staged_path=Path(staged).expanduser())


def clear_restore_marker(settings: Settings) -> None:
    """Remove the restore marker if present (idempotent no-op when absent)."""
    restore_marker_path(settings).unlink(missing_ok=True)


__all__ = [
    "RESTORE_MARKER_FILENAME",
    "RestoreMarker",
    "clear_restore_marker",
    "read_restore_marker",
    "restore_marker_path",
    "write_restore_marker",
]
