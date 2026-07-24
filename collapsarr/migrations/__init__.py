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

from collapsarr.config import Settings

logger = logging.getLogger(__name__)

#: Absolute path to this migrations directory. Doubles as Alembic's
#: ``script_location`` at runtime — resolved from ``__file__`` so it points at
#: the *installed* location inside the wheel, not a repo-relative path.
MIGRATIONS_DIR = Path(__file__).resolve().parent


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


def upgrade_to_head(settings: Settings) -> None:
    """Apply any pending Alembic migrations, bringing the schema up to head.

    This is the single schema-construction routine on the boot path (it replaced
    the retired ``init_db``/``create_all``/``ensure_schema``). On a fresh, empty
    database it runs the full migration chain from base; on an already-current
    database it is a no-op.

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

    if current_revision == head_revision:
        logger.info("Database schema is current (revision %s)", head_revision)
        return

    logger.info(
        "Migrating database schema from %s to %s",
        current_revision or "base (empty database)",
        head_revision,
    )
    command.upgrade(config, "head")


__all__ = ["MIGRATIONS_DIR", "build_alembic_config", "upgrade_to_head"]
