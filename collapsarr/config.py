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
``COLLAPSARR_AUTH_USERNAME``         *(unset)*                  First-boot seed: UI username.
``COLLAPSARR_AUTH_PASSWORD``         *(unset)*                  First-boot seed: UI password.
``COLLAPSARR_AUTH_METHOD``           *(unset)*                  Seed only: forms or basic.
``COLLAPSARR_AUTH_REQUIRED``         *(unset)*                  Seed only: enabled or local_bypass.
``COLLAPSARR_TRUSTED_PROXIES``       *(empty)*                  Trusted reverse-proxy allowlist.
==================================  =========================  =======================

``COLLAPSARR_AUTH_USERNAME``/``COLLAPSARR_AUTH_PASSWORD`` (COL-53) are a
headless-deploy escape hatch: set together and a fresh install seeds that
credential on first boot -- hashed before it's persisted, never stored or
logged in plaintext -- and skips the interactive ``/setup`` gate entirely.
See :func:`collapsarr.settings.env_seed.seed_auth_from_env` for the seeding
logic (one-shot: it never overwrites a credential that already exists, even
if these variables are still set on a later boot) and the README's
Authentication section for the password-recovery/lockout use case.

``COLLAPSARR_TRUSTED_PROXIES`` (COL-112) is a comma-separated allowlist of
IPs/CIDRs (e.g. ``10.0.0.0/8,192.168.1.1``) permitted to sit in front of
Collapsarr as a reverse proxy and supply ``X-Forwarded-For``/
``X-Forwarded-Proto``. Empty by default -- no peer is trusted, so those
headers are ignored wherever they matter. An unparseable entry raises at
``Settings`` construction (fail-fast) rather than being silently dropped. See
:mod:`collapsarr.auth.trust` for the allowlist parsing and the
``resolve_client_address``/``resolve_scheme`` functions that consume it.

``data_dir`` defaults to ``platformdirs.user_data_dir("collapsarr")`` — e.g.
``~/.local/share/collapsarr`` on Linux, native per-OS locations elsewhere —
so a bare-metal/PyPI install has a writable location with no configuration.
The Docker image sets ``COLLAPSARR_DATA_DIR=/config`` explicitly (see
``Dockerfile``), pointing this default at its ``/config`` volume; an existing
``/config`` volume from before this variable existed still resolves its DB to
``/config/collapsarr.db`` via the derivation below, so upgrades need no data
migration.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

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

    auth_username: str | None = Field(
        default=None,
        description=(
            "First-boot credential seed (COL-53): UI username. Set together "
            "with auth_password to skip the interactive /setup gate on a "
            "fresh, declarative deploy (Docker, automation). One-shot: "
            "ignored once a credential already exists, even if this stays "
            "set on a later boot -- not a way to recover a forgotten "
            "password once one is configured, only to bring up a fresh or "
            "locked-out install with a known credential."
        ),
    )
    auth_password: str | None = Field(
        default=None,
        description=(
            "First-boot credential seed (COL-53): UI password, paired with "
            "auth_username. Held here as plaintext only as long as any other "
            "environment variable is -- it is hashed (PBKDF2) before being "
            "persisted, and the plaintext is never stored or logged."
        ),
    )
    auth_method: Literal["forms", "basic"] | None = Field(
        default=None,
        description=(
            "First-boot credential seed (COL-53): auth presentation method "
            "for the seeded credential. Only applied when a credential is "
            "actually seeded (see auth_username/auth_password); falls back "
            "to GlobalSettings' own default (forms) when unset."
        ),
    )
    auth_required: Literal["enabled", "local_bypass"] | None = Field(
        default=None,
        description=(
            "First-boot credential seed (COL-53): required mode for the "
            "seeded credential. Only applied when a credential is actually "
            "seeded (see auth_username/auth_password); falls back to "
            "GlobalSettings' own default (local_bypass) when unset."
        ),
    )
    trusted_proxies: str = Field(
        default="",
        description=(
            "Comma-separated IPs/CIDRs (COL-112) trusted to sit in front of "
            "Collapsarr as a reverse proxy and supply X-Forwarded-For/"
            "X-Forwarded-Proto. Default empty -- no peer is trusted, so "
            "those headers are ignored. See collapsarr.auth.trust for the "
            "allowlist parsing and the resolve_client_address/resolve_scheme "
            "functions that consume it."
        ),
    )

    @model_validator(mode="after")
    def _require_auth_seed_pair(self) -> Settings:
        """Fail fast if only one of the seed username/password is set.

        Both must be provided together to seed a credential (see
        ``collapsarr.settings.env_seed``); a lone
        ``COLLAPSARR_AUTH_USERNAME`` or ``COLLAPSARR_AUTH_PASSWORD`` is
        almost certainly a typo/misconfiguration on a headless deploy, so
        this raises at startup rather than silently seeding nothing.
        """
        if bool(self.auth_username) != bool(self.auth_password):
            raise ValueError(
                "COLLAPSARR_AUTH_USERNAME and COLLAPSARR_AUTH_PASSWORD must "
                "both be set (or both left unset) to seed a first-boot "
                "credential."
            )
        return self

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

    @model_validator(mode="after")
    def _validate_trusted_proxies(self) -> Settings:
        """Fail fast on an unparseable ``COLLAPSARR_TRUSTED_PROXIES`` entry.

        Delegates to :func:`collapsarr.auth.trust.parse_trusted_proxies` --
        the single source of truth for the allowlist grammar -- so a
        malformed IP/CIDR raises here, at startup, instead of being silently
        ignored the first time a request needs the allowlist. Imported
        locally (not at module scope) to avoid a load-time import cycle:
        ``collapsarr.auth.trust`` imports :class:`Settings` from this module
        for its own type hints, but by the time anything actually
        *constructs* a ``Settings`` instance, this module has already
        finished importing, so the deferred import here is safe.
        """
        from .auth.trust import parse_trusted_proxies

        parse_trusted_proxies(self.trusted_proxies)
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
