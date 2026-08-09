"""Contract tests for the About-panel system-info endpoint (COL-123).

Covers ``GET /api/system/info``: the auth gate, the normal response shape
against an injected :class:`~collapsarr.system.probe.SystemProbe` and disk
reading, ``ffmpeg_version: null`` when FFmpeg is unavailable rather than an
error, and ``db_engine`` reporting whatever SQLAlchemy dialect the app's
engine is actually configured with (not assumed to be SQLite) -- the two
"error/edge-case" acceptance criteria the ticket calls out explicitly.
"""

from __future__ import annotations

from typing import NamedTuple

import httpx
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr import __version__
from collapsarr.config import Settings
from collapsarr.health import DiskUsage
from collapsarr.main import create_app
from collapsarr.migrations import build_alembic_config
from collapsarr.settings.service import get_global_settings


class _FakeUsage(NamedTuple):
    total: int
    used: int
    free: int


def _fixed_disk_usage(_path: str) -> DiskUsage:
    """Deterministic disk reading (mirrors ``conftest.py``'s ``_ample_free_space``)."""
    return _FakeUsage(total=2_000_000_000, used=200_000_000, free=1_800_000_000)


def _offline_update_check_transport() -> httpx.MockTransport:
    """A deterministic, offline stand-in for the GitHub Releases fetch (mirrors ``conftest.py``)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="offline in tests")

    return httpx.MockTransport(handler)


class _FakeProbe:
    """A deterministic :class:`~collapsarr.system.probe.SystemProbe` stand-in."""

    def __init__(self, ffmpeg_version: str | None) -> None:
        self._ffmpeg_version = ffmpeg_version

    def python_version(self) -> str:
        return "3.12.4"

    def os_platform(self) -> str:
        return "Linux-test-x86_64-with-glibc2.31"

    def ffmpeg_version(self) -> str | None:
        return self._ffmpeg_version


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _build_client(settings: Settings, *, ffmpeg_version: str | None = "6.1.1") -> TestClient:
    app = create_app(
        settings=settings,
        disk_usage=_fixed_disk_usage,
        update_check_transport=_offline_update_check_transport(),
        system_probe=_FakeProbe(ffmpeg_version),
    )
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #
def test_endpoint_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/system/info").status_code == 401


# --------------------------------------------------------------------------- #
# Normal response: every About field, from the injected probe/disk reading
# --------------------------------------------------------------------------- #
def test_returns_the_about_panel_fields(settings: Settings) -> None:
    with _build_client(settings) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.get("/api/system/info", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "app_version",
            "install_method",
            "python_version",
            "ffmpeg_version",
            "os",
            "db_engine",
            "db_schema_revision",
            "data_dir",
            "database_path",
            "uptime_seconds",
            "timezone",
            "disk",
        }
        assert body["app_version"] == __version__
        assert body["install_method"] in {"docker", "pipx"}
        assert body["python_version"] == "3.12.4"
        assert body["ffmpeg_version"] == "6.1.1"
        assert body["os"] == "Linux-test-x86_64-with-glibc2.31"
        assert body["db_engine"] == "sqlite"
        # A fresh DB is migrated to head by the app's own lifespan.
        assert body["db_schema_revision"]
        assert body["data_dir"] == settings.data_dir
        assert body["database_path"] == settings.database_path
        assert isinstance(body["uptime_seconds"], int | float)
        assert body["uptime_seconds"] >= 0
        assert body["timezone"]
        # Disk numbers come from the injected probe (_fixed_disk_usage), not
        # a real shutil.disk_usage call against the test's tmp_path.
        assert body["disk"] == {"free_bytes": 1_800_000_000, "total_bytes": 2_000_000_000}


# --------------------------------------------------------------------------- #
# FFmpeg missing: null, not an error
# --------------------------------------------------------------------------- #
def test_ffmpeg_missing_reports_null_rather_than_erroring(settings: Settings) -> None:
    with _build_client(settings, ffmpeg_version=None) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.get("/api/system/info", headers=headers)

        assert response.status_code == 200
        assert response.json()["ffmpeg_version"] is None


# --------------------------------------------------------------------------- #
# db_engine reports whatever dialect is actually configured
# --------------------------------------------------------------------------- #
def test_db_engine_reports_whatever_dialect_is_actually_configured(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite is the only backend this repo tests against a real connection
    (``collapsarr/migrations/__init__.py``'s module docstring), so a
    non-SQLite ``DATABASE_URL`` is simulated by patching the already-built
    engine's dialect name directly, rather than standing up a real Postgres
    instance -- the endpoint under test only ever reads
    ``engine.dialect.name``, so this exercises the exact same code path a
    real non-SQLite ``DATABASE_URL`` would.
    """
    with _build_client(settings) as test_client:
        app = test_client.app
        assert isinstance(app, FastAPI)
        monkeypatch.setattr(type(app.state.engine.dialect), "name", "postgresql", raising=False)

        headers = _auth_headers(test_client)
        body = test_client.get("/api/system/info", headers=headers).json()

        assert body["db_engine"] == "postgresql"


# --------------------------------------------------------------------------- #
# db_schema_revision is read live, not the packaged script's head revision
# --------------------------------------------------------------------------- #
def test_db_schema_revision_is_read_live_not_the_packaged_head(settings: Settings) -> None:
    with _build_client(settings) as test_client:
        headers = _auth_headers(test_client)

        config = build_alembic_config(settings)
        head_revision = ScriptDirectory.from_config(config).get_current_head()

        # The app's own lifespan already migrated this DB to head; stamping
        # it one revision behind proves the endpoint reads the DB's live
        # alembic_version rather than always reporting the packaged head.
        command.downgrade(config, "-1")

        body = test_client.get("/api/system/info", headers=headers).json()

        assert body["db_schema_revision"] != head_revision
