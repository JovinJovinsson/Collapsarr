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
``COLLAPSARR_JOB_MAX_CONCURRENCY`` ``1``                      Max downmix jobs run concurrently.
``COLLAPSARR_SCAN_INTERVAL_HOURS`` ``6.0``                    Hours between periodic library scans.
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
    job_max_concurrency: int = Field(
        default=1,
        ge=1,
        description=(
            "Maximum number of downmix jobs the job queue (collapsarr.jobs) runs "
            "concurrently. Read by JobQueue.from_settings()."
        ),
    )
    scan_interval_hours: float = Field(
        default=6.0,
        gt=0,
        description=(
            "Interval, in hours, between periodic full-library scans that enqueue "
            "downmix jobs for monitored files with qualifying targets. Read by the "
            "background scheduler (collapsarr.jobs.scheduler.JobScheduler), which "
            "also uses it as the de-duplication window: a file whose most recent job "
            "reached a terminal state within this window is not re-enqueued, so a "
            "webhook and a scheduled scan overlapping within one cycle can't "
            "double-enqueue it."
        ),
    )

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
