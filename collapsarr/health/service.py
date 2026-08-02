"""Reconcile a tick's health-check results against persisted state (COL-75).

The heart of the framework: :func:`reconcile_health_results` takes the results
every registered check produced this tick and, per *Check Key*
``(code, instance_id)``, diffs them against the persisted
:class:`~collapsarr.health.models.HealthCheckState` rows to detect transitions:

- **pass -> fail** (a key not previously failing is now failing, including the
  very first time a key is seen failing) -> record ``first_failed_at`` and fire
  a ``health_check_failed`` notification.
- **fail -> pass** (a key that was failing now passes) -> clear
  ``first_failed_at`` and fire a ``health_check_recovered`` notification.
- **unchanged** (still failing, or still passing) -> refresh ``message`` /
  ``last_checked_at`` only; **no** notification.

Because state lives in the database, a still-failing check reads ``failing`` on
the first tick after a restart and therefore does **not** re-fire -- the
restart-safety property the FFmpeg startup check never had.

Notifications reuse the generic Connect & Notifications fan-out
(:func:`collapsarr.notify.dispatch_notification`) exactly as the retired
``notify_ffmpeg_missing`` did, and are wrapped so a notifier problem can never
break the scheduler tick.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from collapsarr.notify import NotificationEvent, dispatch_notification, get_notifier_config

from .models import CHECK_STATUS_FAILING, CHECK_STATUS_PASSING, HealthCheckState
from .result import HealthCheckResult

logger = logging.getLogger(__name__)

EVENT_HEALTH_CHECK_FAILED = "health_check_failed"
EVENT_HEALTH_CHECK_RECOVERED = "health_check_recovered"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def list_health_check_states(session: Session) -> list[HealthCheckState]:
    """Return every persisted check-state row, ordered by ``code`` then instance."""
    stmt = select(HealthCheckState).order_by(
        HealthCheckState.code, HealthCheckState.instance_id
    )
    return list(session.execute(stmt).scalars())


def list_failing_checks(session: Session) -> list[HealthCheckState]:
    """Return only the currently-failing check-state rows (populates ``/health``)."""
    return [state for state in list_health_check_states(session) if state.is_failing]


def reconcile_health_results(
    session: Session,
    results: Sequence[HealthCheckResult],
    *,
    now: Callable[[], datetime] = _utcnow,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Persist ``results`` and fire a notification for each state transition.

    ``now`` is the injectable clock stamped onto ``first_failed_at`` /
    ``last_checked_at`` (tests pass a fixed one). ``transport`` is forwarded to
    the notifier dispatch (tests inject an ``httpx.MockTransport``; production
    leaves it ``None`` for a real network call).

    Reconciles purely from the returned results: a check is responsible for
    returning a *passing* result for a key it previously reported failing once
    that key recovers (the FFmpeg check always returns exactly one result, so it
    self-heals; per-instance checks include recovered instances). Keys absent
    from ``results`` are left untouched.
    """
    existing = {(row.code, row.instance_id): row for row in list_health_check_states(session)}
    timestamp = now()
    transitions: list[tuple[HealthCheckResult, str]] = []

    for result in results:
        row = existing.get(result.key)

        if row is None:
            row = HealthCheckState(
                code=result.code,
                instance_id=result.instance_id,
                category=result.category,
                status=CHECK_STATUS_PASSING if result.passing else CHECK_STATUS_FAILING,
                severity=result.severity,
                message=result.message,
                first_failed_at=None if result.passing else timestamp,
                last_checked_at=timestamp,
            )
            session.add(row)
            existing[result.key] = row
            if not result.passing:
                transitions.append((result, EVENT_HEALTH_CHECK_FAILED))
            continue

        was_failing = row.is_failing
        # Live fields refresh every tick regardless of transition.
        row.category = result.category
        row.severity = result.severity
        row.message = result.message
        row.last_checked_at = timestamp

        if result.passing:
            if was_failing:
                row.status = CHECK_STATUS_PASSING
                row.first_failed_at = None
                transitions.append((result, EVENT_HEALTH_CHECK_RECOVERED))
        elif not was_failing:
            row.status = CHECK_STATUS_FAILING
            row.first_failed_at = timestamp
            transitions.append((result, EVENT_HEALTH_CHECK_FAILED))
        # else: still failing -> keep first_failed_at, no notification.

    session.commit()

    if transitions:
        _dispatch_transitions(session, transitions, transport=transport)


def _details(result: HealthCheckResult) -> dict[str, str]:
    details = {
        "code": result.code,
        "category": result.category,
        "severity": result.severity,
    }
    if result.instance_id is not None:
        details["instance_id"] = str(result.instance_id)
    return details


def _build_event(result: HealthCheckResult, event_type: str) -> NotificationEvent:
    if event_type == EVENT_HEALTH_CHECK_RECOVERED:
        title = f"Health check recovered: {result.code}"
    else:
        title = f"Health check failed: {result.code}"
    return NotificationEvent(
        event_type=event_type,
        title=title,
        message=result.message,
        details=_details(result),
    )


def _dispatch_transitions(
    session: Session,
    transitions: Sequence[tuple[HealthCheckResult, str]],
    *,
    transport: httpx.BaseTransport | None,
) -> None:
    """Fan each transition out to every enabled notifier; never raises.

    A notification problem -- reading the config, building the event, or the
    network call -- is caught and logged rather than propagated, so it can never
    break the scheduler tick (the same guarantee the retired
    ``notify_ffmpeg_missing`` gave app startup).
    """
    try:
        config = get_notifier_config(session)
    except Exception:  # noqa: BLE001 - a notification problem must never break a tick
        logger.exception("Failed to read notifier config for health notifications")
        return

    for result, event_type in transitions:
        try:
            dispatch_notification(config, _build_event(result, event_type), transport=transport)
        except Exception:  # noqa: BLE001 - one bad notifier must not break the tick
            logger.exception("Failed to dispatch health notification for %s", result.code)
