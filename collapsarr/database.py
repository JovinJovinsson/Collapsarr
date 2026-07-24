"""Database engine, session factory, and ORM base.

SQLAlchemy 2.0 style. The engine and session factory are built from
:class:`~collapsarr.config.Settings` so tests can point them at a throwaway
SQLite path. Both are attached to ``app.state`` in the application factory and
consumed by request handlers via the :func:`get_session` dependency.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.schema import CreateColumn

from .config import Settings


class Base(DeclarativeBase):
    """Declarative base class shared by all Collapsarr ORM models.

    Feature tickets (instances, tracked media, job queue, settings) define their
    tables against this base so a single ``Base.metadata`` drives schema
    creation.
    """


def create_engine_from_settings(settings: Settings) -> Engine:
    """Create a SQLAlchemy :class:`Engine` from application settings.

    The ``data_dir`` root is created if missing (the platform user-data
    directory on a bare-metal install, or the ``/config`` volume in the
    Docker image) so the app boots with zero configuration. For file-based
    SQLite URLs the database's own parent directory is also created if
    missing (relevant when ``database_path`` points outside ``data_dir``),
    and ``check_same_thread`` is disabled so the connection can be shared
    across FastAPI's worker threads.
    """
    url = settings.sqlalchemy_url
    connect_args: dict[str, object] = {}

    Path(settings.data_dir).expanduser().mkdir(parents=True, exist_ok=True)

    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # Ensure the directory for a file-based SQLite DB exists.
        if settings.database_url is None and settings.database_path != ":memory:":
            db_path = Path(settings.database_path).expanduser()
            db_path.parent.mkdir(parents=True, exist_ok=True)

    return create_engine(url, connect_args=connect_args, future=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a configured :class:`sessionmaker` bound to ``engine``."""
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def init_db(engine: Engine) -> None:
    """Create any tables registered on :class:`Base` that do not yet exist.

    Feature packages (e.g. :mod:`collapsarr.arr`) are imported here, not at
    module scope, so their models register with ``Base.metadata`` before
    ``create_all`` runs without introducing an import-time cycle back to this
    module (they import :class:`Base` from here).
    """
    from . import arr, jobs, media, notify, settings  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_schema(engine)


def ensure_schema(engine: Engine) -> None:
    """Add any model columns missing from an already-created database (COL-48).

    ``create_all`` (see :func:`init_db`) only creates whole tables that don't
    exist yet -- it never alters an existing table to add a column a model
    has gained since that table was first created. This closes that gap: for
    every table already present in the database, it diffs the model's
    columns against the database's actual columns and issues
    ``ALTER TABLE ... ADD COLUMN`` for each one the model has that the
    database doesn't, preserving all existing rows and data.

    Strictly additive -- it never drops, renames, or retypes a column, and a
    column already present is left untouched, so re-running is a no-op. This
    is a deliberate, minimal stopgap; every column added here must be
    nullable or declare a DB-side ``server_default`` (not just an ORM-side
    ``default=``), since SQLite (and most databases) reject adding a
    ``NOT NULL`` column with no default to a table that may already hold
    rows. It will be superseded by the Alembic migration framework (a later
    ticket) once schema changes outgrow "add a column".
    """
    with engine.begin() as connection:
        inspector = inspect(connection)
        existing_tables = set(inspector.get_table_names())
        preparer = connection.dialect.identifier_preparer

        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                # create_all() above should already have created it; skip
                # rather than risk altering a table that isn't there.
                continue

            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue

                column_ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
                quoted_table = preparer.quote(table.name)
                connection.execute(text(f"ALTER TABLE {quoted_table} ADD COLUMN {column_ddl}"))


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped :class:`Session`.

    The session factory is read from ``app.state`` (set up in the application
    factory's lifespan), so each request gets a fresh session that is closed
    when the request finishes.
    """
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    with session_factory() as session:
        yield session
