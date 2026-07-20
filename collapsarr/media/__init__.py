"""Tracked media: files and their per-target/per-language downmix status (COL-25).

Home for tracked-media concerns: a persisted row per known media file, its
per-``(language, target)`` status (missing / processed /
excluded-by-language-filter, per ``docs/plans/2026-07-20-collapsarr-v1-design.md``),
upsert-on-scan-or-webhook semantics, and the "files missing at least one
enabled target" query the future Wanted view (COL-28) reads from.

This module is imported for its side effect of registering
:class:`~collapsarr.media.models.TrackedMediaFile` and
:class:`~collapsarr.media.models.TrackedMediaTargetStatus` with
:data:`collapsarr.database.Base.metadata` -- see
:func:`collapsarr.database.init_db`.
"""

from __future__ import annotations

from .models import MediaTargetStatus, TrackedMediaFile, TrackedMediaTargetStatus
from .service import (
    get_tracked_media,
    list_files_missing_targets,
    list_target_statuses,
    list_tracked_media,
    record_target_processed,
    upsert_tracked_media,
)

__all__ = [
    "MediaTargetStatus",
    "TrackedMediaFile",
    "TrackedMediaTargetStatus",
    "get_tracked_media",
    "list_files_missing_targets",
    "list_target_statuses",
    "list_tracked_media",
    "record_target_processed",
    "upsert_tracked_media",
]
