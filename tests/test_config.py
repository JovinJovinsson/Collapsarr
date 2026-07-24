"""Tests for environment-driven configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from collapsarr import config
from collapsarr.config import Settings
from collapsarr.main import create_app


def test_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Documented defaults apply when no environment is set.

    ``data_dir`` (and, derived from it, ``database_path``) come from
    ``platformdirs.user_data_dir``, which is OS-specific — stub it so the
    assertion is deterministic across CI platforms.
    """
    fake_data_dir = tmp_path / "collapsarr"
    monkeypatch.setattr(config.platformdirs, "user_data_dir", lambda app_name: str(fake_data_dir))

    settings = Settings(_env_file=None)

    assert settings.data_dir == str(fake_data_dir)
    assert settings.database_path == str(fake_data_dir / "collapsarr.db")
    assert settings.host == "0.0.0.0"
    assert settings.port == 8282
    assert settings.log_level == "INFO"
    assert settings.job_max_concurrency == 1
    assert settings.sqlalchemy_url == f"sqlite:///{fake_data_dir / 'collapsarr.db'}"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables override defaults using the COLLAPSARR_ prefix."""
    monkeypatch.setenv("COLLAPSARR_PORT", "9000")
    monkeypatch.setenv("COLLAPSARR_DATABASE_PATH", "/data/test.db")

    settings = Settings(_env_file=None)

    assert settings.port == 9000
    assert settings.database_path == "/data/test.db"
    assert settings.sqlalchemy_url == "sqlite:////data/test.db"


def test_job_max_concurrency_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The job queue's concurrency cap is configurable via the environment."""
    monkeypatch.setenv("COLLAPSARR_JOB_MAX_CONCURRENCY", "4")

    settings = Settings(_env_file=None)

    assert settings.job_max_concurrency == 4


def test_database_url_overrides_path() -> None:
    """An explicit database_url takes precedence over database_path."""
    settings = Settings(
        _env_file=None,
        database_path="/config/collapsarr.db",
        database_url="sqlite:///:memory:",
    )

    assert settings.sqlalchemy_url == "sqlite:///:memory:"


def test_data_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """COLLAPSARR_DATA_DIR overrides the default data_dir root."""
    custom_dir = tmp_path / "custom-data-dir"
    monkeypatch.setenv("COLLAPSARR_DATA_DIR", str(custom_dir))

    settings = Settings(_env_file=None)

    assert settings.data_dir == str(custom_dir)


def test_database_path_derives_from_data_dir(tmp_path: Path) -> None:
    """When database_path isn't set, it resolves inside data_dir."""
    data_dir = tmp_path / "derived"

    settings = Settings(_env_file=None, data_dir=str(data_dir))

    assert settings.database_path == str(data_dir / "collapsarr.db")
    assert settings.sqlalchemy_url == f"sqlite:///{data_dir / 'collapsarr.db'}"


def test_auth_seed_env_vars_are_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No COLLAPSARR_AUTH_* set -- the seed fields stay unset (COL-53)."""
    for var in (
        "COLLAPSARR_AUTH_USERNAME",
        "COLLAPSARR_AUTH_PASSWORD",
        "COLLAPSARR_AUTH_METHOD",
        "COLLAPSARR_AUTH_REQUIRED",
    ):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(_env_file=None)

    assert settings.auth_username is None
    assert settings.auth_password is None
    assert settings.auth_method is None
    assert settings.auth_required is None


def test_auth_seed_env_vars_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """COLLAPSARR_AUTH_* populate the seed fields (COL-53)."""
    monkeypatch.setenv("COLLAPSARR_AUTH_USERNAME", "admin")
    monkeypatch.setenv("COLLAPSARR_AUTH_PASSWORD", "hunter2")
    monkeypatch.setenv("COLLAPSARR_AUTH_METHOD", "basic")
    monkeypatch.setenv("COLLAPSARR_AUTH_REQUIRED", "enabled")

    settings = Settings(_env_file=None)

    assert settings.auth_username == "admin"
    assert settings.auth_password == "hunter2"
    assert settings.auth_method == "basic"
    assert settings.auth_required == "enabled"


def test_auth_seed_requires_username_and_password_together() -> None:
    """A lone COLLAPSARR_AUTH_USERNAME (no password) fails fast at startup."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, auth_username="admin")

    with pytest.raises(ValidationError):
        Settings(_env_file=None, auth_password="hunter2")


def test_auth_seed_method_rejects_an_invalid_value() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            auth_username="admin",
            auth_password="hunter2",
            auth_method="not-a-real-method",
        )


def test_database_path_override_takes_precedence_over_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit COLLAPSARR_DATABASE_PATH wins even when COLLAPSARR_DATA_DIR is set."""
    monkeypatch.setenv("COLLAPSARR_DATA_DIR", str(tmp_path / "data-dir"))
    explicit_path = tmp_path / "elsewhere" / "custom.db"
    monkeypatch.setenv("COLLAPSARR_DATABASE_PATH", str(explicit_path))

    settings = Settings(_env_file=None)

    assert settings.database_path == str(explicit_path)


def test_boots_with_zero_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The app starts with no COLLAPSARR_* environment set and creates its DB.

    Simulates a fresh ``pipx install collapsarr && collapsarr`` on a host with
    no ``/config`` directory: no env vars, no crash, DB lands under the (here,
    stubbed) platform user-data directory.
    """
    for var in ("COLLAPSARR_DATA_DIR", "COLLAPSARR_DATABASE_PATH", "COLLAPSARR_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    fake_data_dir = tmp_path / "no-config-here"
    monkeypatch.setattr(config.platformdirs, "user_data_dir", lambda app_name: str(fake_data_dir))

    settings = Settings(_env_file=None)
    app = create_app(settings=settings)
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200

    # The data dir and DB file were created automatically, with no config.
    assert fake_data_dir.is_dir()
    assert (fake_data_dir / "collapsarr.db").exists()
