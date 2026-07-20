"""Tests for environment-driven configuration."""

from __future__ import annotations

import pytest

from collapsarr.config import Settings


def test_defaults() -> None:
    """Documented defaults apply when no environment is set."""
    settings = Settings(_env_file=None)

    assert settings.database_path == "/config/collapsarr.db"
    assert settings.host == "0.0.0.0"
    assert settings.port == 8282
    assert settings.log_level == "INFO"
    assert settings.sqlalchemy_url == "sqlite:////config/collapsarr.db"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables override defaults using the COLLAPSARR_ prefix."""
    monkeypatch.setenv("COLLAPSARR_PORT", "9000")
    monkeypatch.setenv("COLLAPSARR_DATABASE_PATH", "/data/test.db")

    settings = Settings(_env_file=None)

    assert settings.port == 9000
    assert settings.database_path == "/data/test.db"
    assert settings.sqlalchemy_url == "sqlite:////data/test.db"


def test_database_url_overrides_path() -> None:
    """An explicit database_url takes precedence over database_path."""
    settings = Settings(
        _env_file=None,
        database_path="/config/collapsarr.db",
        database_url="sqlite:///:memory:",
    )

    assert settings.sqlalchemy_url == "sqlite:///:memory:"
