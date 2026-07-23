"""Application configuration.

All settings load from environment variables (prefix ``COLLAPSARR_``) with the
documented defaults below, so a bare ``docker run`` or ``pipx install`` works
without any configuration. An optional ``.env`` file in the working directory is
also read (see ``.env.example``).

Documented environment variables and their defaults:

==================================  =========================  =======================
Environment variable                Default                    Description
==================================  =========================  =======================
``COLLAPSARR_DATA_DIR``              *(OS user-data dir)*       App data root (DB now, logs later).
``COLLAPSARR_DATABASE_PATH``         *(derived from data dir)*  SQLite DB file path.
``COLLAPSARR_DATABASE_URL``          *(derived from path)*      Full SQLAlchemy URL override.
``COLLAPSARR_HOST``                  ``0.0.0.0``                API server bind address.
``COLLAPSARR_PORT``                  ``8282``                   API server bind port.
``COLLAPSARR_LOG_LEVEL``             ``INFO``                   Log level (passed to uvicorn).
``COLLAPSARR_JOB_MAX_CONCURRENCY``   ``1``                      Max concurrent downmix jobs.
``COLLAPSARR_SCAN_INTERVAL_HOURS``   ``6.0``                    Hours between periodic scans.
==================================  =========================  =======================

``data_dir`` defaults to ``platformdirs.user_data_dir("collapsarr")`` — e.g.
``~/.local/share/collapsarr`` on Linux, native per-OS locations elsewhere —
so a bare-metal/PyPI install has a writable location with no configuration.
The Docker image continues to set
``COLLAPSARR_DATABASE_PATH=/config/collapsarr.db`` explicitly (see
``Dockerfile``), which — being a higher-precedence override — is unaffected
by this default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import platformdirs as platformdirs
from pydantic import Field, model_validator
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

    data_dir: str = Field(
        default_factory=lambda: platformdirs.user_data_dir("collapsarr"),
        description=(
            "Root directory for application data. The SQLite database lives "
            "here by default (and, later, logs/backups). Created on startup "
            "if missing. Defaults to the OS-appropriate per-user data "
            "directory (e.g. ~/.local/share/collapsarr on Linux)."
        ),
    )
    database_path: str = Field(
        default="",
        description=(
            "Filesystem path to the SQLite database file. Empty (the "
            "default) resolves to <data_dir>/collapsarr.db; set explicitly "
            "to override, taking precedence over data_dir."
        ),
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

    @model_validator(mode="after")
    def _derive_database_path(self) -> Settings:
        """Resolve an unset ``database_path`` to ``<data_dir>/collapsarr.db``.

        Runs after field validation, so an explicit ``COLLAPSARR_DATABASE_PATH``
        (or a ``database_path`` kwarg) has already populated the field and is
        left untouched — only the empty default is derived from ``data_dir``.
        """
        if not self.database_path:
            self.database_path = str(Path(self.data_dir).expanduser() / "collapsarr.db")
        return self

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
