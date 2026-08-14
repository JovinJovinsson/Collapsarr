"""In-progress guard + persistence for the singleton Self-Update state (COL-230).

:func:`get_self_update_state` is the read/get-or-create seam -- mirrors
:func:`collapsarr.settings.service.get_global_settings`'s get-or-create idiom
rather than :mod:`collapsarr.update_check.service`'s "``None`` until the
first tick" idiom: unlike Update Check state, there is no "first tick" this
row is waiting on -- ``idle``/not-in-progress is a perfectly valid row to
exist before any self-update flow has ever run, and the status endpoint
(:mod:`collapsarr.self_update.routes`) needs a row to read on its very first
call, before COL-232+'s apply flow has ever touched this table.

:func:`begin_self_update` is the in-progress guard's "set" half: it raises
:class:`SelfUpdateAlreadyInProgressError` rather than silently overwriting an
in-flight attempt's state when the guard is already set -- mirroring how
:func:`collapsarr.update_check.service.dismiss_update_check` refuses
(:class:`~collapsarr.update_check.service.UpdateNotAvailableError`) rather
than silently no-oping when its own precondition doesn't hold.
:func:`set_self_update_phase` advances ``phase`` without touching the guard
-- a plain setter; COL-232+ owns deciding when each transition is valid, not
this module. :func:`clear_self_update` is the guard's "clear" half,
idempotent like :func:`collapsarr.update_check.service.undismiss_update_check`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .models import PHASE_DOWNLOADING, PHASE_IDLE, SELF_UPDATE_STATE_ID, SelfUpdateState


class SelfUpdateAlreadyInProgressError(RuntimeError):
    """Refused to start a self-update because one is already in progress (COL-230).

    Raised by :func:`begin_self_update`. Mirrors
    :class:`collapsarr.ffmpeg_download.service.FfmpegExtractionError`'s /
    :class:`collapsarr.backup.service.BackupUnavailableError`'s
    "operation invalid given current runtime state" ``RuntimeError`` usage in
    this codebase, distinct from a ``LookupError``-based "not found" guard
    (there is nothing missing here -- the row exists, its guard is simply
    already held) or a ``ValueError``-based "bad input" guard.
    """


def _utcnow() -> datetime:
    return datetime.now(UTC)


def get_self_update_state(session: Session) -> SelfUpdateState:
    """Return the singleton Self-Update state row, creating it with defaults if absent.

    Safe to call repeatedly and from multiple call sites (the status
    endpoint, the future apply flow's own guard check) -- once created, the
    same row is always returned; it is never recreated or duplicated
    (enforced at the schema level by :class:`~collapsarr.self_update.models.
    SelfUpdateState`'s singleton check constraint). See the module docstring
    for why this is get-or-create rather than "``None`` until the first
    attempt".
    """
    row = session.get(SelfUpdateState, SELF_UPDATE_STATE_ID)
    if row is None:
        row = SelfUpdateState(id=SELF_UPDATE_STATE_ID)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def begin_self_update(
    session: Session,
    *,
    previous_version: str,
    phase: str = PHASE_DOWNLOADING,
    now: Callable[[], datetime] = _utcnow,
) -> SelfUpdateState:
    """Set the in-progress guard and record the version to roll back to (COL-230).

    Raises :class:`SelfUpdateAlreadyInProgressError` when the guard is
    already set -- a self-update flow (COL-232+) must never run two attempts
    concurrently; see the module docstring. On success, stamps
    ``previous_version`` (the running version this attempt could later roll
    back to, consumed by a later rollback ticket), moves ``phase`` to
    ``phase`` (:data:`~collapsarr.self_update.models.PHASE_DOWNLOADING` by
    default -- the first real step of the apply flow), and refreshes
    ``updated_at``.
    """
    row = get_self_update_state(session)
    if row.in_progress:
        raise SelfUpdateAlreadyInProgressError(
            f"A self-update is already in progress (phase={row.phase!r})"
        )
    row.in_progress = True
    row.phase = phase
    row.previous_version = previous_version
    row.updated_at = now()
    session.commit()
    session.refresh(row)
    return row


def set_self_update_phase(
    session: Session, phase: str, *, now: Callable[[], datetime] = _utcnow
) -> SelfUpdateState:
    """Advance the current self-update phase (COL-230).

    A plain setter, callable regardless of the in-progress guard's current
    state -- COL-232+ drives the actual phase transitions (downloading ->
    verifying -> applying -> awaiting_health -> idle/rolled_back) and owns
    deciding when each one is valid to make; this function does not validate
    the transition itself, only persists it and refreshes ``updated_at``.
    """
    row = get_self_update_state(session)
    row.phase = phase
    row.updated_at = now()
    session.commit()
    session.refresh(row)
    return row


def clear_self_update(
    session: Session, *, phase: str = PHASE_IDLE, now: Callable[[], datetime] = _utcnow
) -> SelfUpdateState:
    """Clear the in-progress guard, e.g. on completion, failure, or rollback (COL-230).

    Idempotent -- clearing an already-clear guard just updates ``phase``/
    ``updated_at`` rather than raising, mirroring
    :func:`collapsarr.update_check.service.undismiss_update_check`'s
    idempotent "clear" convention. ``previous_version`` is deliberately left
    untouched here: a completed rollback (``phase=PHASE_ROLLED_BACK``) still
    needs it visible for at least one status read after the guard clears, and
    the next :func:`begin_self_update` call overwrites it unconditionally
    regardless of what it currently holds.
    """
    row = get_self_update_state(session)
    row.in_progress = False
    row.phase = phase
    row.updated_at = now()
    session.commit()
    session.refresh(row)
    return row


__all__ = [
    "SelfUpdateAlreadyInProgressError",
    "begin_self_update",
    "clear_self_update",
    "get_self_update_state",
    "set_self_update_phase",
]
