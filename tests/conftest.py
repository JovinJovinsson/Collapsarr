"""Shared pytest fixtures.

Provides an isolated :class:`~collapsarr.config.Settings` pointed at a temporary
SQLite database, a :class:`~fastapi.testclient.TestClient` built from the
application factory, and a bare :class:`~sqlalchemy.orm.Session` (with schema
already created) for service-layer tests that don't need the HTTP app.
Entering the client's context runs the app's lifespan, so the engine, session
factory, and schema are all set up per test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory, init_db
from collapsarr.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings backed by a throwaway SQLite file under a temp directory."""
    db_path = tmp_path / "collapsarr.db"
    return Settings(database_path=str(db_path))


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A TestClient whose app uses the isolated ``settings`` fixture."""
    app = create_app(settings=settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session(settings: Settings) -> Iterator[Session]:
    """A schema-initialised DB session for service-layer tests (no HTTP app)."""
    engine = create_engine_from_settings(settings)
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as db_session:
        yield db_session
    engine.dispose()
