"""Database-unwritable health check (COL-80).

Filesystem-permission and disk-space checks (COL-79) can both read a
filesystem as healthy while the *database* itself has quietly become
unwritable underneath it -- a stale file lock, a mid-flight permissions
change, or the data volume having been remounted read-only. None of those
show up as "disk full" or "path missing"; the only way to actually notice is
to try a real write and see whether it is accepted.

This check does exactly that, every tick: it performs an actual **write and
commit** against :class:`~collapsarr.health.models.HealthWriteProbe`, a small,
dedicated, otherwise-unused single-row table (COL-80's AC1) -- never a real
config/data table, so this probe can never contend with or corrupt genuine
application state. Because it goes through the ordinary
:class:`~sqlalchemy.orm.Session` the rest of the app already uses (built from
:func:`collapsarr.database.create_engine_from_settings`'s configurable
``sqlalchemy_url``), it works against whatever database backend is actually
configured -- the default file-based SQLite database or otherwise -- with no
backend-specific probe logic (AC3).

A singleton check (Check Key is just the code, ``instance_id=None``) --
there is exactly one database to probe, not one per Arr instance.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .context import HealthCheckContext
from .models import WRITE_PROBE_ID, HealthWriteProbe
from .result import SEVERITY_ERROR, HealthCheckResult

logger = logging.getLogger(__name__)

DATABASE_WRITABLE_CHECK_NAME = "database_writable"
# Follows the `{SEVERITY}-{CATEGORY}-{SEQ}` Check Code glossary format
# (CONTEXT.md). "db" is a new category, distinct from COL-77's `WARN-ARR-001`
# ("arr"), COL-78's `ERR-CONN-001` ("connectivity"), and COL-79's
# `WARN-DISK-001`/`ERR-DISK-001` ("disk") -- this Check Key can never collide
# with any of those. A database that refuses a write is always an `error`
# (never merely a `warning`): there is no degraded-but-still-usable state for
# "the database rejected a commit", so this check has just the one code/
# severity, unlike COL-79's two-tier disk-space check.
DATABASE_UNWRITABLE_CODE = "ERR-DB-001"
DATABASE_WRITABLE_CATEGORY = "db"


def _write_and_commit(session: Session) -> None:
    """Write-and-commit a fresh timestamp to the dedicated probe row.

    Get-or-create against the fixed :data:`~collapsarr.health.models.WRITE_PROBE_ID`,
    matching :func:`collapsarr.settings.service.get_global_settings`'s
    singleton idiom: the row is created once and then updated (never
    re-inserted) on every subsequent tick, so this is a genuine ``UPDATE`` (not
    just an ``INSERT``) after the very first tick.
    """
    probe = session.get(HealthWriteProbe, WRITE_PROBE_ID)
    if probe is None:
        session.add(HealthWriteProbe(id=WRITE_PROBE_ID, pinged_at=datetime.now(UTC)))
    else:
        probe.pinged_at = datetime.now(UTC)
    session.commit()


def run_database_writable_check(context: HealthCheckContext) -> Sequence[HealthCheckResult]:
    """Framework ``run`` callable: write-and-commit to the dedicated probe table.

    Reports the singleton ``ERR-DB-001`` result: passing when the
    write-and-commit is accepted, failing on **any** exception raised while
    attempting it -- deliberately a bare ``Exception`` catch (rather than
    scoping to :class:`sqlalchemy.exc.SQLAlchemyError`) so this reports a
    failure regardless of which layer raises: an ORM-level SQLAlchemy error, a
    backend-specific DBAPI error surfacing through it (SQLite, Postgres,
    MySQL, ...), or a test double standing in for one. On failure the session
    is rolled back so a broken write never leaves this (or a later) tick's
    session in an unusable, still-in-a-failed-transaction state.
    """
    session = context.session
    try:
        _write_and_commit(session)
    except Exception as exc:  # noqa: BLE001 - any error here means "database unwritable"
        logger.warning("Database write-and-commit probe failed: %s", exc)
        session.rollback()
        return [
            HealthCheckResult.failed(
                code=DATABASE_UNWRITABLE_CODE,
                category=DATABASE_WRITABLE_CATEGORY,
                severity=SEVERITY_ERROR,
                message=f"Database write-and-commit probe failed: {exc}",
            )
        ]
    return [
        HealthCheckResult.ok(
            code=DATABASE_UNWRITABLE_CODE,
            category=DATABASE_WRITABLE_CATEGORY,
            severity=SEVERITY_ERROR,
            message="Database write-and-commit probe succeeded.",
        )
    ]


__all__ = [
    "DATABASE_UNWRITABLE_CODE",
    "DATABASE_WRITABLE_CATEGORY",
    "DATABASE_WRITABLE_CHECK_NAME",
    "run_database_writable_check",
]
