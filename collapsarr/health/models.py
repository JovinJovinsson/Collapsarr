"""ORM models for persisted health-check state and probe data (COL-75, COL-80, COL-82).

One row per *Check Key* -- ``(code, instance_id)`` -- so the framework survives
a restart: on boot the scheduler reads the same rows the previous process
wrote and, because a still-failing check's row already reads ``failing``, it
does *not* re-fire a "just failed" notification (see
:mod:`collapsarr.health.service`).

The table is deliberately keyed on ``(code, instance_id)`` rather than a bare
code so it is already shaped for the later per-instance checks (Arr instance
connectivity -- one row per instance): singleton checks (FFmpeg, disk space,
database writability) simply carry ``instance_id = NULL``.

Also defines :class:`HealthWriteProbe` (COL-80): a small, dedicated,
otherwise-unused single-row table the database-unwritable check writes-and-
commits to on every tick, isolated from ``HealthCheckState`` and every real
config/data table so the probe never contends with genuine application
writes.

This module is imported for its side effect of registering both models with
:data:`collapsarr.database.Base.metadata` -- see :mod:`collapsarr.health` and
the Alembic migration environment (:mod:`collapsarr.migrations`).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base

CHECK_STATUS_PASSING = "passing"
CHECK_STATUS_FAILING = "failing"

#: Fixed primary key of the single :class:`HealthWriteProbe` row -- mirrors
#: :data:`collapsarr.settings.models.SETTINGS_ID`'s singleton-row idiom.
WRITE_PROBE_ID = 1


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

    ``dismissed_at`` (COL-82, ``CONTEXT.md``'s "Dismiss (health check)") is set
    when an operator acknowledges this Check Key while it is currently
    failing, and ``None`` otherwise. It hides the row from the ``/health``
    banner (:func:`collapsarr.health.service.list_failing_checks`) while it
    stays visible -- marked dismissed -- on the System > Health list page
    (``GET /api/system/health-checks``). It is automatically cleared the next
    time this Check Key transitions from passing back to failing (see
    :func:`collapsarr.health.service.reconcile_health_results`), so a fresh
    recurrence is never silently hidden behind a stale dismissal.

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
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    @property
    def is_failing(self) -> bool:
        return self.status == CHECK_STATUS_FAILING

    @property
    def is_dismissed(self) -> bool:
        return self.dismissed_at is not None

    def __repr__(self) -> str:
        return (
            f"HealthCheckState(code={self.code!r}, instance_id={self.instance_id!r}, "
            f"status={self.status!r})"
        )


class HealthWriteProbe(Base):
    """The single row the database-unwritable check writes-and-commits to (COL-80).

    Purely a probe target -- it carries no meaning of its own beyond "was a
    write-and-commit against the configured database backend accepted just
    now". Singleton, mirroring :class:`~collapsarr.settings.models.
    GlobalSettings`'s ``id = <fixed id>`` check-constraint idiom: the row is
    created once (the check's first-ever tick) and then updated -- never
    re-inserted -- on every subsequent tick, so a live install exercises a
    genuine ``UPDATE`` (not just an ``INSERT``) almost every time.

    Deliberately its own table, not a column bolted onto ``HealthCheckState``
    or ``global_settings`` -- the whole point of this probe is that it is
    otherwise-unused, so a locked/read-only database is caught by the same
    write path any real feature table would hit, without the probe itself
    ever competing with real application data for the same row/table.
    """

    __tablename__ = "health_write_probe"
    __table_args__ = (
        CheckConstraint(f"id = {WRITE_PROBE_ID}", name="ck_health_write_probe_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=WRITE_PROBE_ID)
    pinged_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    def __repr__(self) -> str:
        return f"HealthWriteProbe(id={self.id!r}, pinged_at={self.pinged_at!r})"
