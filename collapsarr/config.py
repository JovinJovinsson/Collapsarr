"""Application configuration.

All settings load from environment variables (prefix ``COLLAPSARR_``) with the
documented defaults below, so a bare ``docker run`` or ``pip install`` works
without any configuration. An optional ``.env`` file in the working directory is
also read (see ``.env.example``).

Documented environment variables and their defaults:

===============================  ==========================  ==================================
Environment variable             Default                     Description
===============================  ==========================  ==================================
``COLLAPSARR_DATABASE_PATH``     ``/config/collapsarr.db``   Filesystem path to the SQLite DB.
``COLLAPSARR_DATABASE_URL``      *(derived from path)*       Full SQLAlchemy URL override.
``COLLAPSARR_HOST``              ``0.0.0.0``                 Bind address for the API server.
``COLLAPSARR_PORT``              ``8282``                    Bind port for the API server.
``COLLAPSARR_LOG_LEVEL``         ``INFO``                    Log level (passed to uvicorn).
===============================  ==========================  ==================================
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration sourced from the environment.

    Values are read once at process start. Use :func:`get_settings` to obtain
    the cached singleton; construct :class:`Settings` directly (e.g. in tests)
    when you need an isolated, overridden configuration.
    """

    model_config = SettingsConfigDict(
        env_prefix="COLLAPSARR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_path: str = Field(
        default="/config/collapsarr.db",
        description="Filesystem path to the SQLite database file.",
    )
    database_url: str | None = Field(
        default=None,
        description="Full SQLAlchemy database URL. Overrides database_path when set.",
    )

    host: str = Field(default="0.0.0.0", description="API server bind address.")
    port: int = Field(default=8282, description="API server bind port.")
    log_level: str = Field(default="INFO", description="Log level for the server.")

    @property
    def sqlalchemy_url(self) -> str:
        """Resolve the effective SQLAlchemy database URL.

        Uses ``database_url`` verbatim when provided, otherwise builds a SQLite
        URL from ``database_path``.
        """
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.database_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached :class:`Settings` instance."""
    return Settings()
