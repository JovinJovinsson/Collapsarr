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
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.health import DiskUsage
from collapsarr.main import create_app
from collapsarr.migrations import upgrade_to_head


class _FakeDiskUsage(NamedTuple):
    total: int
    used: int
    free: int


def _ample_free_space(_path: str) -> DiskUsage:
    """Disk-usage stand-in reporting 90% free (COL-79).

    The disk-space health check is registered by default, so without this the
    shared ``client`` fixture's startup tick would reflect *this host's real,
    possibly near-full* disk -- making the fixture's health-check-count
    non-deterministic across machines/CI runners. 90% free is comfortably
    clear of the check's default 5%/2% thresholds, keeping the fixture's
    startup tick deterministic the same way a fresh, instance-less database
    keeps the no-Arr-instances check (COL-77) deterministically failing.
    """
    return _FakeDiskUsage(total=1000, used=100, free=900)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings backed by a throwaway SQLite file under a temp directory.

    ``data_dir`` is pinned alongside ``database_path`` (COL-60) so the
    pre-migration backups directory (``data_dir/backups``) also lands under
    ``tmp_path`` -- without this, every test that runs a pending migration
    would write real backup files into the host's actual per-user data
    directory (``platformdirs.user_data_dir``), since ``data_dir`` otherwise
    keeps its process-wide default.
    """
    db_path = tmp_path / "collapsarr.db"
    return Settings(database_path=str(db_path), data_dir=str(tmp_path))


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A TestClient whose app uses the isolated ``settings`` fixture.

    Passes a fixed ``disk_usage`` override (COL-79) so the disk-space health
    check's startup-tick result is deterministic across hosts/CI runners --
    see :func:`_ample_free_space`.
    """
    app = create_app(settings=settings, disk_usage=_ample_free_space)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session(settings: Settings) -> Iterator[Session]:
    """A schema-initialised DB session for service-layer tests (no HTTP app).

    Schema is built by running the Alembic migration chain to head (COL-58) --
    the same mechanism the app boots with -- so tests exercise the real
    migration-built schema, not a ``create_all`` shortcut.
    """
    upgrade_to_head(settings)
    engine = create_engine_from_settings(settings)
    session_factory = create_session_factory(engine)
    with session_factory() as db_session:
        yield db_session
    engine.dispose()
