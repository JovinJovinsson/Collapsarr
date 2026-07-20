"""ORM model for the singleton notifier configuration row (COL-35).

Per the product design (``docs/plans/2026-07-20-collapsarr-v1-design.md``), the
"Connect/notifications" surface is deliberately minimal: a generic webhook and
Discord, each with its own URL + enabled flag, firing on downmix failure and
app health issues (COL-37/COL-38 respectively). Exposing this over HTTP/UI is
a separate ticket (COL-36); this module only owns storage.

Mirrors :class:`collapsarr.settings.models.GlobalSettings`'s singleton
convention: the ``id`` column is constrained to always equal
:data:`NOTIFIER_CONFIG_ID`, so at most one row can ever exist.
:mod:`collapsarr.notify.service` is the only intended way to read or create it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base

NOTIFIER_CONFIG_ID = 1
"""The fixed primary key of the single :class:`NotifierConfig` row."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NotifierConfig(Base):
    """The single row of persisted notifier configuration.

    Both notifiers default to disabled with no URL configured -- notifications
    are opt-in. ``webhook_enabled``/``discord_enabled`` gate whether
    :mod:`collapsarr.notify.dispatch` sends to that notifier at all;
    a notifier can be "enabled" with no URL set (e.g. mid-configuration in the
    UI), in which case dispatch skips it without making a network call rather
    than sending to an empty destination.
    """

    __tablename__ = "notifier_config"
    __table_args__ = (
        CheckConstraint(f"id = {NOTIFIER_CONFIG_ID}", name="ck_notifier_config_singleton"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=NOTIFIER_CONFIG_ID)
    webhook_url: Mapped[str | None] = mapped_column(String(2048), nullable=True, default=None)
    webhook_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discord_webhook_url: Mapped[str | None] = mapped_column(
        String(2048), nullable=True, default=None
    )
    discord_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return (
            f"NotifierConfig(id={self.id!r}, webhook_enabled={self.webhook_enabled!r}, "
            f"discord_enabled={self.discord_enabled!r})"
        )
