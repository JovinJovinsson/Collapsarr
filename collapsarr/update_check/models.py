"""ORM model for the persisted Update Check state (COL-86).

A **singleton** table -- one row, mirroring
:class:`~collapsarr.settings.models.GlobalSettings` /
:class:`~collapsarr.health.models.HealthWriteProbe`'s ``id = <fixed id>``
check-constraint idiom -- rather than :class:`~collapsarr.health.models.
HealthCheckState`'s one-row-per-*Check-Key* shape: there is exactly one
"latest known release" to track, not a set of independently-failing checks.

:class:`UpdateCheckState` caches the last successfully fetched GitHub Release
for the configured channel (``channel``, ``latest_tag``,
``latest_version_label``, ``changelog``, ``published_at``) plus bookkeeping
for when the fetch last ran (``checked_at``) and an operator dismissal of the
current "update available" banner (``dismissed_at`` -- schema-only in this
ticket; no dismiss/undismiss action ships until the API lands, COL-87).
Written every tick by :func:`collapsarr.update_check.service.reconcile_update_check`;
a failed fetch (see :mod:`collapsarr.update_check.client`) refreshes
``checked_at`` only, leaving the last-known-good release data in place rather
than blanking it out.

This module is imported for its side effect of registering the model with
:data:`collapsarr.database.Base.metadata` -- see :mod:`collapsarr.update_check`
and the Alembic migration environment (:mod:`collapsarr.migrations`).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base

#: Fixed primary key of the single :class:`UpdateCheckState` row -- mirrors
#: :data:`collapsarr.settings.models.SETTINGS_ID` /
#: :data:`collapsarr.health.models.WRITE_PROBE_ID`'s singleton-row idiom.
UPDATE_CHECK_STATE_ID = 1


class UpdateCheckState(Base):
    """The single persisted row of Update Check state.

    ``channel`` records which channel (``stable``|``beta``) the row's data
    reflects -- the configured :attr:`~collapsarr.settings.models.
    GlobalSettings.update_channel` at the time of the tick that wrote it.
    ``latest_tag``/``latest_version_label``/``changelog``/``published_at``
    are ``None`` until the first successful fetch. ``checked_at`` advances on
    *every* tick (success or failure) so "last checked" is always accurate;
    the other fields only change on a successful fetch.
    """

    __tablename__ = "update_check_state"
    __table_args__ = (
        CheckConstraint(f"id = {UPDATE_CHECK_STATE_ID}", name="ck_update_check_state_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=UPDATE_CHECK_STATE_ID)
    channel: Mapped[str | None] = mapped_column(String(10), nullable=True, default=None)
    latest_tag: Mapped[str | None] = mapped_column(String(100), nullable=True, default=None)
    latest_version_label: Mapped[str | None] = mapped_column(
        String(200), nullable=True, default=None
    )
    changelog: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    def __repr__(self) -> str:
        return (
            f"UpdateCheckState(channel={self.channel!r}, latest_tag={self.latest_tag!r}, "
            f"checked_at={self.checked_at!r})"
        )


__all__ = ["UPDATE_CHECK_STATE_ID", "UpdateCheckState"]
