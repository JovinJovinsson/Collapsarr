"""Tests for persisted notifier config: defaults, read, update (COL-35).

Uses the shared ``session`` fixture (a schema-initialised DB session -- see
``conftest.py``), matching the pattern in ``test_settings_service.py``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from collapsarr.notify.models import NotifierConfig
from collapsarr.notify.service import get_notifier_config, update_notifier_config

# ---------------------------------------------------------------------------
# Default creation on first run.
# ---------------------------------------------------------------------------


def test_get_notifier_config_creates_the_row_on_first_call(session: Session) -> None:
    assert session.scalars(select(NotifierConfig)).one_or_none() is None

    config = get_notifier_config(session)

    assert config.id == 1
    assert session.scalars(select(NotifierConfig)).one().id == config.id


def test_get_notifier_config_defaults_are_disabled_with_no_url(session: Session) -> None:
    config = get_notifier_config(session)

    assert config.webhook_url is None
    assert config.webhook_enabled is False
    assert config.discord_webhook_url is None
    assert config.discord_enabled is False


def test_get_notifier_config_does_not_duplicate_the_row_across_calls(session: Session) -> None:
    first = get_notifier_config(session)
    second = get_notifier_config(session)

    assert first.id == second.id
    assert session.scalars(select(NotifierConfig)).all() == [first]


# ---------------------------------------------------------------------------
# Read.
# ---------------------------------------------------------------------------


def test_get_notifier_config_reads_back_a_previously_created_row(session: Session) -> None:
    created = get_notifier_config(session)
    session.expunge(created)

    read_back = get_notifier_config(session)

    assert read_back.id == created.id
    assert read_back.webhook_enabled == created.webhook_enabled


# ---------------------------------------------------------------------------
# Update.
# ---------------------------------------------------------------------------


def test_update_notifier_config_changes_only_the_given_fields(session: Session) -> None:
    get_notifier_config(session)  # seed defaults first

    updated = update_notifier_config(
        session, webhook_url="https://example.com/hook", webhook_enabled=True
    )

    assert updated.webhook_url == "https://example.com/hook"
    assert updated.webhook_enabled is True
    # Untouched fields keep their defaults.
    assert updated.discord_webhook_url is None
    assert updated.discord_enabled is False


def test_update_notifier_config_creates_the_row_if_absent(session: Session) -> None:
    assert session.scalars(select(NotifierConfig)).one_or_none() is None

    updated = update_notifier_config(session, webhook_enabled=True)

    assert updated.id == 1
    assert updated.webhook_enabled is True


def test_update_notifier_config_persists_across_a_fresh_read(session: Session) -> None:
    update_notifier_config(
        session,
        discord_webhook_url="https://discord.com/api/webhooks/1/abc",
        discord_enabled=True,
    )

    reread = get_notifier_config(session)

    assert reread.discord_webhook_url == "https://discord.com/api/webhooks/1/abc"
    assert reread.discord_enabled is True


def test_update_notifier_config_explicit_none_clears_webhook_url(session: Session) -> None:
    update_notifier_config(session, webhook_url="https://example.com/hook")

    cleared = update_notifier_config(session, webhook_url=None)

    assert cleared.webhook_url is None


def test_update_notifier_config_explicit_none_clears_discord_url(session: Session) -> None:
    update_notifier_config(session, discord_webhook_url="https://discord.com/api/webhooks/1/abc")

    cleared = update_notifier_config(session, discord_webhook_url=None)

    assert cleared.discord_webhook_url is None


def test_update_notifier_config_omitted_url_field_stays_untouched(session: Session) -> None:
    update_notifier_config(session, webhook_url="https://example.com/hook")

    unchanged = update_notifier_config(session, webhook_enabled=True)

    assert unchanged.webhook_url == "https://example.com/hook"
    assert unchanged.webhook_enabled is True


def test_update_notifier_config_can_disable_an_enabled_notifier(session: Session) -> None:
    update_notifier_config(session, webhook_enabled=True)

    disabled = update_notifier_config(session, webhook_enabled=False)

    assert disabled.webhook_enabled is False


# ---------------------------------------------------------------------------
# Public re-exports.
# ---------------------------------------------------------------------------


def test_notifier_config_importable_from_package_root() -> None:
    """Sanity check the public re-exports from collapsarr.notify."""
    from collapsarr.notify import NotifierConfig as ReexportedNotifierConfig
    from collapsarr.notify import get_notifier_config as reexported_get
    from collapsarr.notify import update_notifier_config as reexported_update

    assert ReexportedNotifierConfig is NotifierConfig
    assert reexported_get is get_notifier_config
    assert reexported_update is update_notifier_config
