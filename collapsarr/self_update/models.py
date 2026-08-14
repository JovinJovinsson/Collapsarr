"""ORM model for the persisted Self-Update state (COL-230, Epic COL-224).

A **singleton** table -- one row, mirroring
:class:`~collapsarr.settings.models.GlobalSettings` /
:class:`~collapsarr.health.models.HealthWriteProbe`'s ``id = <fixed id>``
check-constraint idiom (see :mod:`collapsarr.update_check.models`'s module
docstring, which this one deliberately follows) -- rather than
:class:`~collapsarr.health.models.HealthCheckState`'s one-row-per-*Check-Key*
shape: there is exactly one self-update attempt that can ever be in flight at
a time, not a set of independently-tracked attempts.

:class:`SelfUpdateState` tracks the three things every self-update flow
(COL-232 pipx apply, later native/rollback tickets) needs to coordinate
through: an **in-progress guard** (``in_progress``) so two attempts can never
run concurrently, the **current phase** (``phase``) so a status/liveness
endpoint (:mod:`collapsarr.self_update.routes`) and the frontend's future
polling screen can report progress, and the **previous version** to roll back
to (``previous_version``) if the newly-applied build fails its post-update
health check.

Phase vocabulary (:data:`SELF_UPDATE_PHASES`) -- chosen to fit the flow
COL-232 onward implements: fetch the release archive (``downloading``),
checksum-verify it (``verifying``), install it in place (``applying``), wait
for the re-exec'd/restarted process to report healthy (``awaiting_health``),
and -- only if that health check fails -- revert to ``previous_version``
(``rolled_back``). ``idle`` is both the initial state (no attempt has ever
run) and the terminal *success* state (:func:`~collapsarr.self_update.
service.clear_self_update`'s default) once a self-update completes and the
new version is confirmed healthy; ``rolled_back`` is the terminal *failure*
state. This ticket only defines the vocabulary and the guard -- the actual
phase transitions are driven by COL-232 onward via
:func:`~collapsarr.self_update.service.set_self_update_phase`.

This module is imported for its side effect of registering the model with
:data:`collapsarr.database.Base.metadata` -- see :mod:`collapsarr.self_update`
and the Alembic migration environment (:mod:`collapsarr.migrations`).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base

#: Fixed primary key of the single :class:`SelfUpdateState` row -- mirrors
#: :data:`collapsarr.settings.models.SETTINGS_ID` /
#: :data:`collapsarr.update_check.models.UPDATE_CHECK_STATE_ID`'s
#: singleton-row idiom.
SELF_UPDATE_STATE_ID = 1

PHASE_IDLE = "idle"
PHASE_DOWNLOADING = "downloading"
PHASE_VERIFYING = "verifying"
PHASE_APPLYING = "applying"
PHASE_AWAITING_HEALTH = "awaiting_health"
PHASE_ROLLED_BACK = "rolled_back"

#: Every valid :attr:`SelfUpdateState.phase` value, in flow order -- see the
#: module docstring for what each one means. Not a DB-level ``Enum`` column
#: (plain ``String``, matching :class:`~collapsarr.update_check.models.
#: UpdateCheckState`'s ``channel``/:class:`~collapsarr.plex.models.
#: ConnectivityStatus` precedent of validating in Python rather than at the
#: schema level for a value set that may still grow as COL-232-236 land).
SELF_UPDATE_PHASES = (
    PHASE_IDLE,
    PHASE_DOWNLOADING,
    PHASE_VERIFYING,
    PHASE_APPLYING,
    PHASE_AWAITING_HEALTH,
    PHASE_ROLLED_BACK,
)


class SelfUpdateState(Base):
    """The single persisted row of Self-Update state.

    ``in_progress`` is the guard :func:`~collapsarr.self_update.service.
    begin_self_update` sets and :func:`~collapsarr.self_update.
    service.clear_self_update` clears -- read live by the status endpoint and
    by COL-232+'s own re-entrancy check before starting a new attempt.
    ``phase`` defaults to :data:`PHASE_IDLE` and is only meaningful to read
    precisely while ``in_progress`` is ``True`` (or immediately after a
    ``rolled_back`` completion); it is not reset to ``idle`` automatically
    just because ``in_progress`` is ``False`` on a fresh row.
    ``previous_version`` is ``None`` until the first self-update attempt
    ever begins (:func:`~collapsarr.self_update.service.begin_self_update`
    stamps the running version at that moment), then holds whichever version
    the most recent attempt could roll back to. ``updated_at`` advances on
    every guard/phase write, giving the status endpoint a "how fresh is this"
    signal for a future stuck/stale-attempt detection.
    """

    __tablename__ = "self_update_state"
    __table_args__ = (
        CheckConstraint(f"id = {SELF_UPDATE_STATE_ID}", name="ck_self_update_state_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=SELF_UPDATE_STATE_ID)
    in_progress: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False, default=PHASE_IDLE)
    previous_version: Mapped[str | None] = mapped_column(String(100), nullable=True, default=None)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    def __repr__(self) -> str:
        return (
            f"SelfUpdateState(in_progress={self.in_progress!r}, phase={self.phase!r}, "
            f"previous_version={self.previous_version!r})"
        )


__all__ = [
    "PHASE_APPLYING",
    "PHASE_AWAITING_HEALTH",
    "PHASE_DOWNLOADING",
    "PHASE_IDLE",
    "PHASE_ROLLED_BACK",
    "PHASE_VERIFYING",
    "SELF_UPDATE_PHASES",
    "SELF_UPDATE_STATE_ID",
    "SelfUpdateState",
]
