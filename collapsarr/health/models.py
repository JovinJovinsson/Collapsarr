"""ORM model for persisted per-check health state (COL-75).

One row per *Check Key* -- ``(code, instance_id)`` -- so the framework survives
a restart: on boot the scheduler reads the same rows the previous process
wrote and, because a still-failing check's row already reads ``failing``, it
does *not* re-fire a "just failed" notification (see
:mod:`collapsarr.health.service`).

The table is deliberately keyed on ``(code, instance_id)`` rather than a bare
code so it is already shaped for the later per-instance checks (Arr instance
connectivity -- one row per instance): singleton checks (FFmpeg, disk space,
database writability) simply carry ``instance_id = NULL``.

This module is imported for its side effect of registering
:class:`HealthCheckState` with :data:`collapsarr.database.Base.metadata` -- see
:mod:`collapsarr.health` and the Alembic migration environment
(:mod:`collapsarr.migrations`).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base

CHECK_STATUS_PASSING = "passing"
CHECK_STATUS_FAILING = "failing"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HealthCheckState(Base):
    """The persisted state of one health check, keyed by its *Check Key*.

    ``status`` is ``"passing"`` or ``"failing"``. ``first_failed_at`` is set
    when the row transitions into ``failing`` and cleared on recovery, so it
    records the *start* of the current failing streak (not each tick).
    ``last_checked_at`` advances every tick the check runs. ``severity`` and
    ``category`` mirror the check's authored metadata; ``message`` is the latest
    detail from the check.

    Note on the ``(code, instance_id)`` uniqueness: SQLite treats ``NULL`` as
    distinct in a ``UNIQUE`` constraint, so the constraint does not by itself
    prevent two singleton rows sharing a code. Uniqueness is instead guaranteed
    by :func:`collapsarr.health.service.reconcile_health_results`, which always
    looks a key up before inserting; the constraint documents the intent and
    enforces it for the non-NULL (per-instance) case.
    """

    __tablename__ = "health_check_state"
    __table_args__ = (
        UniqueConstraint("code", "instance_id", name="uq_health_check_state_code_instance"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    first_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None
    )
    last_checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    @property
    def is_failing(self) -> bool:
        return self.status == CHECK_STATUS_FAILING

    def __repr__(self) -> str:
        return (
            f"HealthCheckState(code={self.code!r}, instance_id={self.instance_id!r}, "
            f"status={self.status!r})"
        )
