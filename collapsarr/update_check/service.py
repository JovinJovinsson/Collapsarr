"""Persist a tick's fetched GitHub Release into the singleton Update Check state (COL-86).

:func:`reconcile_update_check` is the persistence half of the Update Check
scheduler's tick, mirroring how :func:`collapsarr.health.service.
reconcile_health_results` is the persistence half of the health scheduler's
tick -- but simpler: there is one singleton row (not one per Check Key), and
no notification dispatch in this slice (that's a separate, later ticket per
the spec; an "update available" is informational, not a failure state -- see
``CONTEXT.md``'s Update Check entry).

``checked_at`` advances on *every* call (a fetch attempt happened, whether or
not it succeeded). The release-describing fields (``latest_tag``,
``latest_version_label``, ``changelog``, ``published_at``) are only
overwritten on a **successful** fetch (``result.ok``) -- a transient network
failure must not blank out the last-known-good "latest available version"
just because this tick's GitHub call failed, mirroring
:mod:`collapsarr.update_check.client`'s "never raises, returns a no-result
signal instead" contract: the signal is handled here by simply not touching
the cached data, not by propagating an error.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .client import GitHubReleaseResult
from .models import UPDATE_CHECK_STATE_ID, UpdateCheckState

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def get_update_check_state(session: Session) -> UpdateCheckState | None:
    """Return the singleton Update Check state row, or ``None`` before the first tick."""
    return session.get(UpdateCheckState, UPDATE_CHECK_STATE_ID)


def reconcile_update_check(
    session: Session,
    result: GitHubReleaseResult,
    channel: str,
    *,
    now: Callable[[], datetime] = _utcnow,
) -> UpdateCheckState:
    """Persist ``result`` (this tick's GitHub fetch outcome) into the singleton row.

    Creates the row on the very first tick if it doesn't exist yet. ``channel``
    is always recorded (the configured channel at the time of this tick, even
    on a failed fetch, so the row always reflects "what channel is this data
    for" accurately). A failed fetch is logged and otherwise a no-op beyond
    stamping ``checked_at`` -- see the module docstring.
    """
    row = session.get(UpdateCheckState, UPDATE_CHECK_STATE_ID)
    if row is None:
        row = UpdateCheckState(id=UPDATE_CHECK_STATE_ID)
        session.add(row)

    timestamp = now()
    row.channel = channel
    row.checked_at = timestamp

    if result.ok:
        row.latest_tag = result.tag
        row.latest_version_label = result.name
        row.changelog = result.body
        row.published_at = result.published_at
    else:
        logger.warning("Update Check fetch failed: %s", result.error)

    session.commit()
    return row


__all__ = ["get_update_check_state", "reconcile_update_check"]
