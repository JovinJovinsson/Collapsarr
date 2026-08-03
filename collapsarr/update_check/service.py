"""Persist a tick's fetched GitHub Release into the singleton Update Check state (COL-86, COL-89).

:func:`reconcile_update_check` is the persistence half of the Update Check
scheduler's tick, mirroring how :func:`collapsarr.health.service.
reconcile_health_results` is the persistence half of the health scheduler's
tick -- but simpler: there is one singleton row (not one per Check Key).

``checked_at`` advances on *every* call (a fetch attempt happened, whether or
not it succeeded). The release-describing fields (``latest_tag``,
``latest_version_label``, ``changelog``, ``published_at``) are only
overwritten on a **successful** fetch (``result.ok``) -- a transient network
failure must not blank out the last-known-good "latest available version"
just because this tick's GitHub call failed, mirroring
:mod:`collapsarr.update_check.client`'s "never raises, returns a no-result
signal instead" contract: the signal is handled here by simply not touching
the cached data, not by propagating an error.

**Edge-triggered notification (COL-89).** A ``NotificationEvent`` (event type
:data:`EVENT_UPDATE_AVAILABLE`) fires via the generic Connect & Notifications
fan-out (:func:`collapsarr.notify.dispatch_notification`) *only* when this
tick's successfully-fetched ``latest_tag`` differs from the previously-stored
one -- never on a tick where the tag is unchanged (including a tick that
merely refreshes ``checked_at``) and never on a failed fetch. That same
transition resets ``dismissed_at`` to ``NULL``, mirroring
:func:`collapsarr.health.service.reconcile_health_results`'s pass -> fail
handling: a genuinely new release is a fresh occurrence, so a stale dismissal
from a prior "update available" notice must not silently hide it. Dispatch is
wrapped so a notifier problem can never break the scheduler tick, same as the
health framework.

**Dismiss / undismiss (COL-89, ``CONTEXT.md``'s "Dismiss (health check)"
pattern, applied to the singleton Update Check row).**
:func:`dismiss_update_check` lets an operator acknowledge the current "update
available" notice (stamping ``dismissed_at``); :func:`undismiss_update_check`
clears it early. Because :func:`reconcile_update_check` above auto-clears
``dismissed_at`` the moment ``latest_tag`` next changes, a dismissal only ever
silences the *current* known release -- an even newer one always re-surfaces.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from collapsarr.notify import NotificationEvent, dispatch_notification, get_notifier_config

from .client import GitHubReleaseResult
from .models import UPDATE_CHECK_STATE_ID, UpdateCheckState

logger = logging.getLogger(__name__)

EVENT_UPDATE_AVAILABLE = "update_available"


class UpdateCheckStateNotFoundError(LookupError):
    """No :class:`~collapsarr.update_check.models.UpdateCheckState` row exists yet (COL-89).

    Raised by :func:`dismiss_update_check` / :func:`undismiss_update_check`
    when called before the very first Update Check tick has ever run -- there
    is nothing to (un)dismiss yet.
    """


def _utcnow() -> datetime:
    return datetime.now(UTC)


def get_update_check_state(session: Session) -> UpdateCheckState | None:
    """Return the singleton Update Check state row, or ``None`` before the first tick."""
    return session.get(UpdateCheckState, UPDATE_CHECK_STATE_ID)


def _get_update_check_state_or_raise(session: Session) -> UpdateCheckState:
    row = get_update_check_state(session)
    if row is None:
        raise UpdateCheckStateNotFoundError("No Update Check state exists yet")
    return row


def dismiss_update_check(
    session: Session, *, now: Callable[[], datetime] = _utcnow
) -> UpdateCheckState:
    """Dismiss the current "update available" notice (COL-89).

    Stamps ``dismissed_at`` on the singleton row. Idempotent: dismissing an
    already-dismissed row just refreshes the timestamp. Raises
    :class:`UpdateCheckStateNotFoundError` if no tick has ever run.
    """
    row = _get_update_check_state_or_raise(session)
    row.dismissed_at = now()
    session.commit()
    return row


def undismiss_update_check(session: Session) -> UpdateCheckState:
    """Clear a dismissal on the singleton Update Check row early (COL-89).

    Idempotent: undismissing a row that isn't dismissed is a no-op. Raises
    :class:`UpdateCheckStateNotFoundError` if no tick has ever run.
    """
    row = _get_update_check_state_or_raise(session)
    row.dismissed_at = None
    session.commit()
    return row


def reconcile_update_check(
    session: Session,
    result: GitHubReleaseResult,
    channel: str,
    *,
    now: Callable[[], datetime] = _utcnow,
    transport: httpx.BaseTransport | None = None,
) -> UpdateCheckState:
    """Persist ``result`` (this tick's GitHub fetch outcome) into the singleton row.

    Creates the row on the very first tick if it doesn't exist yet. ``channel``
    is always recorded (the configured channel at the time of this tick, even
    on a failed fetch, so the row always reflects "what channel is this data
    for" accurately). A failed fetch is logged and otherwise a no-op beyond
    stamping ``checked_at`` -- see the module docstring.

    ``transport`` is forwarded to the notifier dispatch (tests inject an
    ``httpx.MockTransport``; production leaves it ``None`` for a real network
    call) -- see the module docstring's "Edge-triggered notification" section
    for when a notification actually fires.
    """
    row = session.get(UpdateCheckState, UPDATE_CHECK_STATE_ID)
    if row is None:
        row = UpdateCheckState(id=UPDATE_CHECK_STATE_ID)
        session.add(row)

    previous_tag = row.latest_tag
    timestamp = now()
    row.channel = channel
    row.checked_at = timestamp

    transitioned = False
    if result.ok:
        if result.tag != previous_tag:
            transitioned = True
            # COL-89: a genuinely new release is a fresh occurrence -- clear
            # any dismissal left over from a prior "update available" notice
            # so this new one isn't silently hidden behind it.
            row.dismissed_at = None
        row.latest_tag = result.tag
        row.latest_version_label = result.name
        row.changelog = result.body
        row.published_at = result.published_at
    else:
        logger.warning("Update Check fetch failed: %s", result.error)

    session.commit()

    if transitioned:
        _dispatch_update_available(session, row, transport=transport)

    return row


def _build_event(row: UpdateCheckState) -> NotificationEvent:
    title = f"Update available: {row.latest_version_label or row.latest_tag}"
    message = f"A new Collapsarr release is available: {row.latest_tag}"
    details = {"channel": row.channel or ""}
    if row.latest_tag is not None:
        details["latest_tag"] = row.latest_tag
    return NotificationEvent(
        event_type=EVENT_UPDATE_AVAILABLE,
        title=title,
        message=message,
        details=details,
    )


def _dispatch_update_available(
    session: Session,
    row: UpdateCheckState,
    *,
    transport: httpx.BaseTransport | None,
) -> None:
    """Fan the "update available" transition out to every enabled notifier; never raises.

    A notification problem -- reading the config, building the event, or the
    network call -- is caught and logged rather than propagated, so it can
    never break the scheduler tick, mirroring
    :func:`collapsarr.health.service._dispatch_transitions`.
    """
    try:
        config = get_notifier_config(session)
    except Exception:  # noqa: BLE001 - a notification problem must never break a tick
        logger.exception("Failed to read notifier config for update-available notification")
        return

    try:
        dispatch_notification(config, _build_event(row), transport=transport)
    except Exception:  # noqa: BLE001 - one bad notifier must not break the tick
        logger.exception("Failed to dispatch update-available notification")


__all__ = [
    "EVENT_UPDATE_AVAILABLE",
    "UpdateCheckStateNotFoundError",
    "dismiss_update_check",
    "get_update_check_state",
    "reconcile_update_check",
    "undismiss_update_check",
]
