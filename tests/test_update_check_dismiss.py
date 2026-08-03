"""Tests for Update Check dismiss/undismiss (COL-89).

``dismiss_update_check`` / ``undismiss_update_check`` (``collapsarr.
update_check.service``) let an operator acknowledge the current "update
available" notice so it stops nagging until the notice actually changes.
Mirrors ``tests/test_health_dismiss.py``'s structure, adapted for the
singleton Update Check row (no per-Check-Key id -- there's only ever one row
to (un)dismiss).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from collapsarr.settings.models import UPDATE_CHANNEL_STABLE
from collapsarr.update_check.client import GitHubReleaseResult
from collapsarr.update_check.service import (
    UpdateCheckStateNotFoundError,
    dismiss_update_check,
    get_update_check_state,
    reconcile_update_check,
    undismiss_update_check,
)


def _seed(session: Session, tag: str = "v1.0.0") -> None:
    reconcile_update_check(session, GitHubReleaseResult(ok=True, tag=tag), UPDATE_CHANNEL_STABLE)


def _dismissed_at(session: Session) -> datetime | None:
    row = get_update_check_state(session)
    assert row is not None
    return row.dismissed_at


# ---------------------------------------------------------------------------
# AC: dismissing sets dismissed_at; undismissing clears it.
# ---------------------------------------------------------------------------


def test_dismiss_sets_dismissed_at(session: Session) -> None:
    _seed(session)

    dismissed = dismiss_update_check(session)

    assert dismissed.dismissed_at is not None
    assert _dismissed_at(session) is not None


def test_dismiss_is_idempotent(session: Session) -> None:
    _seed(session)
    dismiss_update_check(session)

    dismissed_again = dismiss_update_check(session)

    assert dismissed_again.dismissed_at is not None


def test_dismiss_raises_when_no_tick_has_ever_run(session: Session) -> None:
    with pytest.raises(UpdateCheckStateNotFoundError):
        dismiss_update_check(session)


def test_undismiss_clears_dismissed_at(session: Session) -> None:
    _seed(session)
    dismiss_update_check(session)

    undismissed = undismiss_update_check(session)

    assert undismissed.dismissed_at is None
    assert _dismissed_at(session) is None


def test_undismiss_is_idempotent_on_an_already_undismissed_row(session: Session) -> None:
    _seed(session)

    result = undismiss_update_check(session)  # never dismissed to begin with

    assert result.dismissed_at is None


def test_undismiss_raises_when_no_tick_has_ever_run(session: Session) -> None:
    with pytest.raises(UpdateCheckStateNotFoundError):
        undismiss_update_check(session)


# ---------------------------------------------------------------------------
# AC: a dismissed notice automatically reappears once an even newer version
# is published (the same reconcile transition logic clears the dismissal).
# ---------------------------------------------------------------------------


def test_a_dismissed_notice_reappears_when_a_newer_tag_is_published(session: Session) -> None:
    _seed(session, tag="v1.0.0")
    dismiss_update_check(session)
    assert _dismissed_at(session) is not None

    _seed(session, tag="v2.0.0")  # a fresh reconcile tick, newer tag

    row = get_update_check_state(session)
    assert row is not None
    assert row.dismissed_at is None
    assert row.latest_tag == "v2.0.0"


def test_a_dismissed_notice_stays_dismissed_while_the_tag_is_unchanged(session: Session) -> None:
    _seed(session, tag="v1.0.0")
    dismiss_update_check(session)

    _seed(session, tag="v1.0.0")  # same tag, another tick

    row = get_update_check_state(session)
    assert row is not None
    assert row.dismissed_at is not None
