"""Alembic migration environment for Collapsarr (COL-57).

Alembic loads this file (by path) for both authoring commands
(``alembic revision --autogenerate`` / ``alembic history``, driven by the
repo-root authoring ``alembic.ini`` against a scratch SQLite DB) and — from
COL-58 onward — the runtime upgrade driven by the programmatically-built Config
in :func:`collapsarr.migrations.build_alembic_config`. It never reads
``alembic.ini`` on its own: the database URL is taken from whichever Config
invoked it.

SQLite is the only supported/tested backend. ``render_as_batch=True`` works
around SQLite's limited ``ALTER TABLE``; ``compare_type`` and
``compare_server_default`` make autogenerate notice column type / server-default
drift against ``Base.metadata``.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from collapsarr.database import Base

# Import every feature package for its side effect of registering models with
# Base.metadata (the same pattern as collapsarr.database.init_db), so
# autogenerate and upgrade see the full current schema.
import collapsarr.arr  # noqa: F401,E402  isort:skip
import collapsarr.jobs  # noqa: F401,E402  isort:skip
import collapsarr.media  # noqa: F401,E402  isort:skip
import collapsarr.notify  # noqa: F401,E402  isort:skip
import collapsarr.settings  # noqa: F401,E402  isort:skip

config = context.config

# Only configure logging from a file when one was supplied (authoring runs via
# alembic.ini). The runtime Config has no file, so this is skipped.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL against a URL, no DBAPI)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
