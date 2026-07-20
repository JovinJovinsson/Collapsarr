"""ORM model for the singleton global application settings row (COL-24).

Per the product design (``docs/plans/2026-07-20-collapsarr-v1-design.md``),
Settings covers: enabled downmix targets, language allow-list, codec/bitrate
overrides, concurrency limit, and the UI auth toggle. (Notification config --
webhook/Discord -- is a separate, dedicated model owned by the "Connect &
Notifications" epic's own ticket, COL-35; it is deliberately not part of this
row.)

:class:`~collapsarr.downmix.targets.DownmixTarget` is reused rather than
inventing a parallel enum -- :mod:`collapsarr.downmix.targets` already notes
that its plain-dataclass ``DownmixSettings`` is "settings-*shaped*" and meant
to be adapted into a real, persisted model later; this is that adaptation.
``enabled_targets``/``language_allow_list`` are stored as comma-joined
strings (same convention :mod:`collapsarr.jobs.history` uses for its
``target``/``language`` columns) rather than a JSON column, since nothing
else in this codebase uses one yet.

``GlobalSettings`` is a **singleton** table: the ``id`` column is
constrained to always equal :data:`SETTINGS_ID`, so at most one row can ever
exist (a second row would need a second, distinct primary key value, which
the check constraint forbids). :mod:`collapsarr.settings.service` is the
only intended way to read or create that row.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base
from collapsarr.downmix.targets import DownmixTarget

SETTINGS_ID = 1
"""The fixed primary key of the single :class:`GlobalSettings` row."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GlobalSettings(Base):
    """The single row of global, persisted application settings.

    Defaults match the PRD exactly: Stereo enabled by default with 2.1/5.1
    opt-in (``enabled_targets`` defaults to just ``"stereo"``), AAC for the
    Stereo target, AC3 @ 448kbps for the surround targets, a concurrency
    limit of 1 (matching :attr:`collapsarr.config.Settings.job_max_concurrency`'s
    own default), and UI auth disabled.

    ``language_allow_list`` of ``None`` (the default) means "no allow-list --
    evaluate every language present on a file", matching
    :attr:`~collapsarr.downmix.targets.DownmixSettings.language_allow_list`.
    """

    __tablename__ = "global_settings"
    __table_args__ = (
        CheckConstraint(f"id = {SETTINGS_ID}", name="ck_global_settings_singleton"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=SETTINGS_ID)
    enabled_targets: Mapped[str] = mapped_column(
        String(50), nullable=False, default=DownmixTarget.STEREO.value
    )
    language_allow_list: Mapped[str | None] = mapped_column(
        String(500), nullable=True, default=None
    )
    stereo_codec: Mapped[str] = mapped_column(String(50), nullable=False, default="aac")
    stereo_bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    surround_codec: Mapped[str] = mapped_column(String(50), nullable=False, default="ac3")
    surround_bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True, default=448)
    concurrency_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    ui_auth_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return (
            f"GlobalSettings(id={self.id!r}, enabled_targets={self.enabled_targets!r}, "
            f"concurrency_limit={self.concurrency_limit!r}, "
            f"ui_auth_enabled={self.ui_auth_enabled!r})"
        )
