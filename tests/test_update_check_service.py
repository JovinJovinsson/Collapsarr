"""Tests for the reconcile/read service (COL-86, edge-triggered notification COL-89).

Drives :func:`reconcile_update_check` directly against a
:class:`~collapsarr.update_check.client.GitHubReleaseResult` (no HTTP), the
same "service layer, no transport" idiom
:mod:`tests.test_health_service` (implicitly, via ``test_health_scheduler``)
uses for :func:`collapsarr.health.service.reconcile_health_results`.

The COL-89 notification tests below inject an ``httpx.MockTransport`` for the
notifier dispatch only -- ``reconcile_update_check`` never makes the GitHub
fetch itself (that's :mod:`collapsarr.update_check.client`, driven by the
scheduler), so there is only ever one transport to inject here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.migrations import upgrade_to_head
from collapsarr.notify.service import update_notifier_config
from collapsarr.settings.models import UPDATE_CHANNEL_STABLE
from collapsarr.update_check.client import GitHubReleaseResult
from collapsarr.update_check.service import get_update_check_state, reconcile_update_check

_FIXED_NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def db_session(settings: Settings) -> Iterator[Session]:
    upgrade_to_head(settings)
    engine = create_engine_from_settings(settings)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        yield session
    engine.dispose()


def test_no_state_before_first_reconcile(db_session: Session) -> None:
    assert get_update_check_state(db_session) is None


def test_successful_result_persists_release_data(db_session: Session) -> None:
    result = GitHubReleaseResult(
        ok=True,
        tag="v1.2.3",
        name="Collapsarr v1.2.3",
        body="- fixed things",
        published_at=_FIXED_NOW,
    )

    reconcile_update_check(db_session, result, UPDATE_CHANNEL_STABLE, now=lambda: _FIXED_NOW)

    row = get_update_check_state(db_session)
    assert row is not None
    assert row.channel == UPDATE_CHANNEL_STABLE
    assert row.latest_tag == "v1.2.3"
    assert row.latest_version_label == "Collapsarr v1.2.3"
    assert row.changelog == "- fixed things"
    assert row.published_at == _FIXED_NOW.replace(tzinfo=None)
    assert row.checked_at == _FIXED_NOW.replace(tzinfo=None)


def test_checked_at_advances_on_every_call(db_session: Session) -> None:
    result = GitHubReleaseResult(ok=True, tag="v1.0.0")
    reconcile_update_check(db_session, result, UPDATE_CHANNEL_STABLE, now=lambda: _FIXED_NOW)

    later = _FIXED_NOW.replace(hour=13)
    reconcile_update_check(db_session, result, UPDATE_CHANNEL_STABLE, now=lambda: later)

    row = get_update_check_state(db_session)
    assert row is not None
    assert row.checked_at == later.replace(tzinfo=None)


def test_failed_fetch_does_not_clobber_previously_known_good_data(db_session: Session) -> None:
    good = GitHubReleaseResult(ok=True, tag="v1.2.3", name="v1.2.3", body="notes")
    reconcile_update_check(db_session, good, UPDATE_CHANNEL_STABLE, now=lambda: _FIXED_NOW)

    failed = GitHubReleaseResult(ok=False, error="network error")
    later = _FIXED_NOW.replace(hour=13)
    reconcile_update_check(db_session, failed, UPDATE_CHANNEL_STABLE, now=lambda: later)

    row = get_update_check_state(db_session)
    assert row is not None
    # Last-known-good release data survives the failed tick...
    assert row.latest_tag == "v1.2.3"
    assert row.latest_version_label == "v1.2.3"
    assert row.changelog == "notes"
    # ...but checked_at still advances, recording that an attempt happened.
    assert row.checked_at == later.replace(tzinfo=None)


def test_failed_fetch_before_any_success_persists_no_release_data(db_session: Session) -> None:
    failed = GitHubReleaseResult(ok=False, error="network error")

    reconcile_update_check(db_session, failed, UPDATE_CHANNEL_STABLE, now=lambda: _FIXED_NOW)

    row = get_update_check_state(db_session)
    assert row is not None
    assert row.latest_tag is None
    assert row.checked_at == _FIXED_NOW.replace(tzinfo=None)


def test_channel_is_always_recorded_even_on_failure(db_session: Session) -> None:
    failed = GitHubReleaseResult(ok=False, error="network error")

    reconcile_update_check(db_session, failed, "beta", now=lambda: _FIXED_NOW)

    row = get_update_check_state(db_session)
    assert row is not None
    assert row.channel == "beta"


# --------------------------------------------------------------------------- #
# COL-89: edge-triggered notification -- fires only on a latest_tag change.
# --------------------------------------------------------------------------- #


def _capture_transport() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    return httpx.MockTransport(handler), seen


def _enable_notifier(session: Session) -> None:
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)


def test_notifies_on_the_very_first_successful_fetch(db_session: Session) -> None:
    _enable_notifier(db_session)
    transport, seen = _capture_transport()

    reconcile_update_check(
        db_session,
        GitHubReleaseResult(ok=True, tag="v1.0.0"),
        UPDATE_CHANNEL_STABLE,
        now=lambda: _FIXED_NOW,
        transport=transport,
    )

    assert len(seen) == 1


def test_notifies_once_when_latest_tag_changes_and_not_again_while_unchanged(
    db_session: Session,
) -> None:
    _enable_notifier(db_session)
    transport, seen = _capture_transport()

    reconcile_update_check(
        db_session,
        GitHubReleaseResult(ok=True, tag="v1.0.0"),
        UPDATE_CHANNEL_STABLE,
        now=lambda: _FIXED_NOW,
        transport=transport,
    )
    assert len(seen) == 1

    # Same tag, several more ticks -- no new notification.
    later = _FIXED_NOW.replace(hour=13)
    reconcile_update_check(
        db_session,
        GitHubReleaseResult(ok=True, tag="v1.0.0"),
        UPDATE_CHANNEL_STABLE,
        now=lambda: later,
        transport=transport,
    )
    reconcile_update_check(
        db_session,
        GitHubReleaseResult(ok=True, tag="v1.0.0"),
        UPDATE_CHANNEL_STABLE,
        now=lambda: later,
        transport=transport,
    )
    assert len(seen) == 1

    # A genuinely new tag -- fires again.
    reconcile_update_check(
        db_session,
        GitHubReleaseResult(ok=True, tag="v2.0.0"),
        UPDATE_CHANNEL_STABLE,
        now=lambda: later,
        transport=transport,
    )
    assert len(seen) == 2


def test_a_failed_fetch_never_fires_a_notification(db_session: Session) -> None:
    _enable_notifier(db_session)
    transport, seen = _capture_transport()

    reconcile_update_check(
        db_session,
        GitHubReleaseResult(ok=False, error="network error"),
        UPDATE_CHANNEL_STABLE,
        now=lambda: _FIXED_NOW,
        transport=transport,
    )

    assert len(seen) == 0


def test_a_latest_tag_transition_clears_a_prior_dismissal(db_session: Session) -> None:
    reconcile_update_check(
        db_session,
        GitHubReleaseResult(ok=True, tag="v1.0.0"),
        UPDATE_CHANNEL_STABLE,
        now=lambda: _FIXED_NOW,
    )
    row = get_update_check_state(db_session)
    assert row is not None
    row.dismissed_at = _FIXED_NOW
    db_session.commit()

    reconcile_update_check(
        db_session,
        GitHubReleaseResult(ok=True, tag="v2.0.0"),
        UPDATE_CHANNEL_STABLE,
        now=lambda: _FIXED_NOW,
    )

    row = get_update_check_state(db_session)
    assert row is not None
    assert row.dismissed_at is None


def test_an_unchanged_tag_never_auto_clears_a_dismissal(db_session: Session) -> None:
    reconcile_update_check(
        db_session,
        GitHubReleaseResult(ok=True, tag="v1.0.0"),
        UPDATE_CHANNEL_STABLE,
        now=lambda: _FIXED_NOW,
    )
    row = get_update_check_state(db_session)
    assert row is not None
    row.dismissed_at = _FIXED_NOW
    db_session.commit()

    reconcile_update_check(
        db_session,
        GitHubReleaseResult(ok=True, tag="v1.0.0"),
        UPDATE_CHANNEL_STABLE,
        now=lambda: _FIXED_NOW,
    )

    row = get_update_check_state(db_session)
    assert row is not None
    assert row.dismissed_at is not None
