"""Service-layer read/write interface for notifier config (COL-35).

Plain functions taking a SQLAlchemy :class:`~sqlalchemy.orm.Session`, matching
the pattern already used by :mod:`collapsarr.settings.service`. HTTP exposure
(a future Connect settings page) is COL-36's concern -- this module is the
whole storage surface.

:func:`get_notifier_config` is get-or-create: it returns the singleton row,
creating it with defaults (both notifiers disabled, no URL) on first call if
it doesn't exist yet. :func:`update_notifier_config` changes only the fields
passed explicitly; the two URL fields use the :data:`_UNSET` sentinel (the
same convention :mod:`collapsarr.settings.service` uses for its nullable
fields) so "not provided" can be told apart from "explicitly clear this URL".
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .models import NOTIFIER_CONFIG_ID, NotifierConfig


class _Unset:
    """Sentinel type distinguishing an omitted keyword argument from ``None``."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNSET"


_UNSET = _Unset()


def get_notifier_config(session: Session) -> NotifierConfig:
    """Return the singleton notifier config row, creating it with defaults if absent.

    Safe to call repeatedly and from multiple call sites -- once created, the
    same row is always returned; it is never recreated or duplicated (enforced
    at the schema level by :class:`~collapsarr.notify.models.NotifierConfig`'s
    singleton check constraint).
    """
    config = session.get(NotifierConfig, NOTIFIER_CONFIG_ID)
    if config is None:
        config = NotifierConfig(id=NOTIFIER_CONFIG_ID)
        session.add(config)
        session.commit()
        session.refresh(config)
    return config


def update_notifier_config(
    session: Session,
    *,
    webhook_url: str | None | _Unset = _UNSET,
    webhook_enabled: bool | None = None,
    discord_webhook_url: str | None | _Unset = _UNSET,
    discord_enabled: bool | None = None,
) -> NotifierConfig:
    """Update the given fields on the notifier config row and return it.

    Only fields passed explicitly are changed. For the two URL fields, passing
    ``None`` explicitly *clears* the stored URL -- omitting the argument (the
    default) leaves it untouched. Creates the row with defaults first if it
    doesn't exist yet, same as :func:`get_notifier_config`.
    """
    config = get_notifier_config(session)

    if not isinstance(webhook_url, _Unset):
        config.webhook_url = webhook_url
    if webhook_enabled is not None:
        config.webhook_enabled = webhook_enabled
    if not isinstance(discord_webhook_url, _Unset):
        config.discord_webhook_url = discord_webhook_url
    if discord_enabled is not None:
        config.discord_enabled = discord_enabled

    session.commit()
    session.refresh(config)
    return config
