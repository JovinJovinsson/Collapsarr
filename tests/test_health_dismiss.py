"""Tests for health-check dismiss/undismiss (COL-82).

``dismiss_health_check`` / ``undismiss_health_check`` (``collapsarr.health.
service``) let an operator acknowledge a specific, currently-failing Check Key
so it drops off ``list_failing_checks`` (the ``/health`` banner's source) while
remaining visible -- marked dismissed -- in ``list_health_check_states`` (the
System > Health list page's source). ``reconcile_health_results`` auto-clears
the dismissal the moment that Check Key next transitions from passing back to
failing, so a fresh recurrence is never silently hidden behind a stale
dismissal (see ``CONTEXT.md``'s "Dismiss (health check)").
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from collapsarr.health import (
    SEVERITY_ERROR,
    HealthCheckNotFailingError,
    HealthCheckResult,
    HealthCheckStateNotFoundError,
    dismiss_health_check,
    list_failing_checks,
    list_health_check_states,
    reconcile_health_results,
    undismiss_health_check,
)


def _failing(code: str = "TEST-001", *, instance_id: int | None = None) -> HealthCheckResult:
    return HealthCheckResult.failed(
        code=code, category="test", severity=SEVERITY_ERROR, message="down", instance_id=instance_id
    )


def _passing(code: str = "TEST-001", *, instance_id: int | None = None) -> HealthCheckResult:
    return HealthCheckResult.ok(
        code=code, category="test", severity=SEVERITY_ERROR, message="up", instance_id=instance_id
    )


# ---------------------------------------------------------------------------
# AC: dismiss hides it from list_failing_checks (the /health banner's source).
# ---------------------------------------------------------------------------


def test_dismissing_a_failing_check_hides_it_from_the_failing_list(session: Session) -> None:
    reconcile_health_results(session, [_failing()])
    row = list_health_check_states(session)[0]
    assert row in list_failing_checks(session)

    dismissed = dismiss_health_check(session, row.id)

    assert dismissed.dismissed_at is not None
    assert dismissed.is_dismissed is True
    # Hidden from the failing list (the banner)...
    assert list_failing_checks(session) == []
    # ...but still present, and still failing, on the full list (the page).
    all_states = list_health_check_states(session)
    assert len(all_states) == 1
    assert all_states[0].is_failing is True
    assert all_states[0].dismissed_at is not None


def test_dismiss_refuses_a_currently_passing_check(session: Session) -> None:
    reconcile_health_results(session, [_passing()])
    row = list_health_check_states(session)[0]

    with pytest.raises(HealthCheckNotFailingError):
        dismiss_health_check(session, row.id)

    # Refusal must not have mutated the row.
    assert list_health_check_states(session)[0].dismissed_at is None


def test_dismiss_raises_for_an_unknown_id(session: Session) -> None:
    with pytest.raises(HealthCheckStateNotFoundError):
        dismiss_health_check(session, 999999)


# ---------------------------------------------------------------------------
# AC: a still-failing check stays dismissed across ticks.
# ---------------------------------------------------------------------------


def test_a_still_failing_check_stays_dismissed_across_ticks(session: Session) -> None:
    reconcile_health_results(session, [_failing()])
    row = list_health_check_states(session)[0]
    dismiss_health_check(session, row.id)

    # Several more ticks, still failing every time.
    reconcile_health_results(session, [_failing()])
    reconcile_health_results(session, [_failing()])

    row = list_health_check_states(session)[0]
    assert row.status == "failing"
    assert row.dismissed_at is not None
    assert list_failing_checks(session) == []


# ---------------------------------------------------------------------------
# AC: a pass -> fail transition after a prior dismissal clears it.
# ---------------------------------------------------------------------------


def test_pass_to_fail_transition_after_a_prior_dismissal_clears_it(session: Session) -> None:
    reconcile_health_results(session, [_failing()])
    row = list_health_check_states(session)[0]
    dismiss_health_check(session, row.id)
    assert list_health_check_states(session)[0].dismissed_at is not None

    # Recovers...
    reconcile_health_results(session, [_passing()])
    # ...then fails again: a fresh occurrence, the stale dismissal is cleared.
    reconcile_health_results(session, [_failing()])

    row = list_health_check_states(session)[0]
    assert row.status == "failing"
    assert row.dismissed_at is None
    assert row in list_failing_checks(session)


def test_still_failing_never_auto_clears_a_dismissal(session: Session) -> None:
    """Sanity check the auto-clear is scoped to pass -> fail, not every tick."""
    reconcile_health_results(session, [_failing()])
    row = list_health_check_states(session)[0]
    dismiss_health_check(session, row.id)

    reconcile_health_results(session, [_failing()])  # still failing, no transition

    assert list_health_check_states(session)[0].dismissed_at is not None


# ---------------------------------------------------------------------------
# undismiss: clears early, idempotent, works regardless of current status.
# ---------------------------------------------------------------------------


def test_undismiss_clears_a_dismissal_early(session: Session) -> None:
    reconcile_health_results(session, [_failing()])
    row = list_health_check_states(session)[0]
    dismiss_health_check(session, row.id)

    undismissed = undismiss_health_check(session, row.id)

    assert undismissed.dismissed_at is None
    assert [r.id for r in list_failing_checks(session)] == [row.id]


def test_undismiss_is_idempotent_on_an_already_undismissed_row(session: Session) -> None:
    reconcile_health_results(session, [_failing()])
    row = list_health_check_states(session)[0]

    result = undismiss_health_check(session, row.id)  # never dismissed to begin with

    assert result.dismissed_at is None


def test_undismiss_works_regardless_of_current_status(session: Session) -> None:
    reconcile_health_results(session, [_failing()])
    row = list_health_check_states(session)[0]
    dismiss_health_check(session, row.id)

    # Recovers while still dismissed -- undismiss must still succeed.
    reconcile_health_results(session, [_passing()])
    undismissed = undismiss_health_check(session, row.id)

    assert undismissed.dismissed_at is None


def test_undismiss_raises_for_an_unknown_id(session: Session) -> None:
    with pytest.raises(HealthCheckStateNotFoundError):
        undismiss_health_check(session, 999999)


# ---------------------------------------------------------------------------
# Per-instance scoping: dismissing one instance's row leaves a sibling alone.
# ---------------------------------------------------------------------------


def test_dismiss_is_scoped_to_one_instance_row(session: Session) -> None:
    reconcile_health_results(
        session,
        [_failing("ERR-CONN-001", instance_id=1), _failing("ERR-CONN-001", instance_id=2)],
    )
    rows = {(r.code, r.instance_id): r for r in list_health_check_states(session)}

    dismiss_health_check(session, rows[("ERR-CONN-001", 1)].id)

    failing_ids = {(r.code, r.instance_id) for r in list_failing_checks(session)}
    assert ("ERR-CONN-001", 1) not in failing_ids
    assert ("ERR-CONN-001", 2) in failing_ids
