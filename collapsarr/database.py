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
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    """Declarative base class shared by all Collapsarr ORM models.

    Feature tickets (instances, tracked media, job queue, settings) define their
    tables against this base so a single ``Base.metadata`` drives schema
    creation.
    """


def create_engine_from_settings(settings: Settings) -> Engine:
    """Create a SQLAlchemy :class:`Engine` from application settings.

    For file-based SQLite URLs the parent directory is created if missing (the
    ``/config`` volume in the Docker image), and ``check_same_thread`` is
    disabled so the connection can be shared across FastAPI's worker threads.
    """
    url = settings.sqlalchemy_url
    connect_args: dict[str, object] = {}

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
    from . import arr, jobs  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped :class:`Session`.

    The session factory is read from ``app.state`` (set up in the application
    factory's lifespan), so each request gets a fresh session that is closed
    when the request finishes.
    """
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    with session_factory() as session:
        yield session
