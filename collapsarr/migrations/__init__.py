"""Alembic migration package for Collapsarr (COL-57).

Ships *inside* the installed package so the migration environment (``env.py``),
the revision template (``script.py.mako``), and the versioned migrations under
``versions/`` are all present in the built wheel / Docker image — not just in a
source checkout. This module also builds the runtime Alembic
:class:`~alembic.config.Config` programmatically from :class:`Settings`, so the
app never reads an ``alembic.ini`` at runtime (the repo-root ``alembic.ini`` is
an authoring-only convenience for ``alembic revision --autogenerate`` /
``alembic history``).

SQLite is the only supported and tested backend; see ``README.md`` in this
directory for the migration-authoring workflow.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from collapsarr.config import Settings

logger = logging.getLogger(__name__)

#: Absolute path to this migrations directory. Doubles as Alembic's
#: ``script_location`` at runtime — resolved from ``__file__`` so it points at
#: the *installed* location inside the wheel, not a repo-relative path.
MIGRATIONS_DIR = Path(__file__).resolve().parent

#: Baseline revision (the schema a ``create_all``-era database already has). An
#: unversioned-but-populated DB is stamped here to adopt its existing schema
#: without rebuilding it; see :func:`upgrade_to_head`.
BASELINE_REVISION = "afde30c41b7b"

#: Sentinel table proving an unversioned database is a real, populated Collapsarr
#: install (not an empty file). Its presence is what distinguishes "adopt this
#: existing schema" from "build from base". ``global_settings`` is the singleton
#: settings row every install has.
SENTINEL_TABLE = "global_settings"


def build_alembic_config(settings: Settings) -> Config:
    """Build a runtime Alembic :class:`~alembic.config.Config` from ``settings``.

    No ``alembic.ini`` is read: ``script_location`` is pinned to this packaged
    directory and ``sqlalchemy.url`` comes from
    :attr:`Settings.sqlalchemy_url`. This is the Config the application hands to
    ``alembic.command.upgrade(config, "head")`` (see :func:`upgrade_to_head`).
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url)
    return config


def _current_revision(settings: Settings) -> str | None:
    """Return the revision the database is stamped at, or ``None`` if empty.

    A brand-new (fresh-install) database has no ``alembic_version`` row yet, so
    this reports ``None`` — the signal that the full chain from base needs to
    run.
    """
    # Reuse the app's engine builder so the data dir / SQLite parent exist and
    # the SQLite ``check_same_thread`` tweak is applied. Imported here (not at
    # module scope) to keep import order simple — ``database`` only depends on
    # ``config``, so there is no cycle.
    from collapsarr.database import create_engine_from_settings

    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _has_sentinel_table(settings: Settings) -> bool:
    """Return whether the sentinel table (:data:`SENTINEL_TABLE`) exists.

    Used only when the DB is unversioned (no ``alembic_version``): a populated
    install has this table (adopt at baseline), a truly empty file does not
    (build from base).
    """
    from collapsarr.database import create_engine_from_settings

    engine = create_engine_from_settings(settings)
    try:
        return inspect(engine).has_table(SENTINEL_TABLE)
    finally:
        engine.dispose()


def upgrade_to_head(settings: Settings) -> None:
    """Apply any pending Alembic migrations, bringing the schema up to head.

    This is the single schema-construction routine on the boot path (it replaced
    the retired ``init_db``/``create_all``/``ensure_schema``). Three cases:

    * **Fresh, empty database** — no ``alembic_version`` and no sentinel table:
      run the full migration chain from base.
    * **Unversioned but populated** (a ``create_all``-era install: no
      ``alembic_version`` but the sentinel :data:`SENTINEL_TABLE` exists):
      *stamp* the baseline revision to adopt the existing schema without
      rebuilding it, then upgrade to head so any post-baseline deltas (e.g. the
      reconcile-indexes heal) apply. This is COL-59's existing-DB adoption.
    * **Already versioned** — has ``alembic_version``: run only pending deltas
      (a no-op when already at head).

    Logging: emits the from→to revisions when a migration actually runs, and
    "schema is current" when there is nothing to do.

    Fail-fast: any error raised by Alembic propagates to the caller unchanged.
    Wired into the application lifespan, that means a failed migration aborts
    startup (non-zero exit) and the app never serves against a half-migrated
    schema.
    """
    config = build_alembic_config(settings)
    head_revision = ScriptDirectory.from_config(config).get_current_head()
    current_revision = _current_revision(settings)

    # Adopt an unversioned-but-populated (create_all-era) database: stamp the
    # baseline it already matches, then let the normal upgrade apply new deltas.
    # Guarded on current_revision being None, so it never re-stamps a versioned
    # DB — the stamp is idempotent by construction.
    if current_revision is None and _has_sentinel_table(settings):
        logger.info(
            "Adopting unversioned populated database: stamping baseline %s",
            BASELINE_REVISION,
        )
        command.stamp(config, BASELINE_REVISION)
        current_revision = BASELINE_REVISION

    if current_revision == head_revision:
        logger.info("Database schema is current (revision %s)", head_revision)
        return

    logger.info(
        "Migrating database schema from %s to %s",
        current_revision or "base (empty database)",
        head_revision,
    )
    command.upgrade(config, "head")


__all__ = [
    "BASELINE_REVISION",
    "MIGRATIONS_DIR",
    "SENTINEL_TABLE",
    "build_alembic_config",
    "upgrade_to_head",
]
