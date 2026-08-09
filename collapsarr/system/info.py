"""GET /api/system/info -- environment/runtime facts for the About panel (COL-123).

A thin, read-only ``/api/system`` aggregation view -- same shape as
:mod:`collapsarr.system.tasks` (COL-122) -- over facts that already live
elsewhere: :data:`collapsarr.__version__`,
:func:`~collapsarr.update_check.environment.is_docker_environment`, the
injectable :class:`~collapsarr.system.probe.SystemProbe` (Python version / OS
platform / FFmpeg version), the live database's applied Alembic revision, and
the disk-space health check's :func:`~collapsarr.health.disk_space.get_disk_usage`
probe.

Kept in its own module, separate from :mod:`collapsarr.system.tasks`
(COL-122's Scheduled Task registry), purely so the two tickets can land
commits on the same Epic branch without touching the same file -- not a
deeper architectural split; both are thin aggregation views reading each
subsystem's existing state directly rather than a shared abstraction, per
``docs/adr/0005-system-tasks-endpoint-not-shared-scheduler.md``.

Endpoint:

* ``GET /api/system/info`` -- one :class:`SystemInfoRead` row of "About"
  facts driving the new Status page's About panel.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Literal

from alembic.runtime.migration import MigrationContext
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import __version__
from ..config import Settings
from ..database import get_session
from ..health.disk_space import get_disk_usage
from ..update_check.environment import is_docker_environment
from .probe import SystemProbe

router = APIRouter(prefix="/api/system", tags=["system"])

InstallMethod = Literal["docker", "pipx"]
"""The two install methods reported by the About panel (CONTEXT.md's "Install
Method") -- spelled out as literals (not plain ``str``) so mypy rejects an
unrecognised value, matching :data:`collapsarr.settings.routes.
AuthRequiredMode`/:data:`~collapsarr.settings.routes.AuthMethodMode`'s
precedent for a closed, named mode field. ``"docker"`` when ``/.dockerenv``
is present, otherwise ``"pipx"`` -- the primary documented bare-metal install
path (README.md); pip-from-source (``pip install -e``) is a dev-only variant
of the same non-Docker case and isn't distinguished separately, matching how
``GET /api/system/updates``' ``is_docker`` boolean already collapses
"not Docker" to one bucket."""

INSTALL_METHOD_DOCKER: InstallMethod = "docker"
INSTALL_METHOD_PIPX: InstallMethod = "pipx"


# --- schemas -----------------------------------------------------------------


class DiskUsageRead(BaseModel):
    """Raw free/total byte counts for the filesystem backing ``data_dir``."""

    free_bytes: int
    total_bytes: int


class SystemInfoRead(BaseModel):
    """Response shape for the About panel (``GET /api/system/info``)."""

    app_version: str
    install_method: InstallMethod
    python_version: str
    ffmpeg_version: str | None
    os: str
    db_engine: str
    db_schema_revision: str | None
    data_dir: str
    database_path: str
    uptime_seconds: float
    timezone: str
    disk: DiskUsageRead


# --- helpers -------------------------------------------------------------


def _install_method() -> InstallMethod:
    """``"docker"`` under Docker (``/.dockerenv`` present), else ``"pipx"``."""
    return INSTALL_METHOD_DOCKER if is_docker_environment() else INSTALL_METHOD_PIPX


def _db_schema_revision(session: Session) -> str | None:
    """The database's actually-applied Alembic revision, read live off ``session``.

    Deliberately *not* the packaged migration script's head revision
    (:func:`collapsarr.migrations.get_current_head`-equivalent) -- reading
    the live ``alembic_version`` table via
    :class:`~alembic.runtime.migration.MigrationContext` (mirrors
    :func:`collapsarr.migrations._current_revision`, applied here to the
    request's own session/connection instead of a fresh engine) is what
    actually answers "what schema is this database at right now", which can
    differ from this build's packaged head if an operator is running an
    older Collapsarr build against a database a newer build already
    migrated. ``None`` for a database with no ``alembic_version`` row at all
    (shouldn't happen on a running app, whose lifespan always migrates to
    head before serving requests, but reported as ``None`` rather than
    raising).
    """
    return MigrationContext.configure(session.connection()).get_current_revision()


def _current_timezone() -> str:
    """The server process's local timezone name/abbreviation (e.g. ``"UTC"``, ``"PST"``).

    :meth:`~datetime.datetime.tzname` on an aware "now" reads whatever the
    OS/``TZ`` environment currently reports, correctly reflecting DST when
    applicable -- falls back to ``"UTC"`` on the rare platform where it
    reports nothing.
    """
    name = datetime.now().astimezone().tzname()
    return name or "UTC"


def _uptime_seconds(request: Request) -> float:
    """Seconds since :func:`collapsarr.main.create_app`'s lifespan started.

    Computed from a :func:`time.monotonic` stamp
    (``app.state.process_start_monotonic``) rather than wall-clock time, so a
    system clock adjustment (NTP sync, DST) can't produce a negative or
    jumping reading. ``0.0`` if the stamp is somehow missing (lifespan never
    ran) rather than raising.
    """
    start: float | None = getattr(request.app.state, "process_start_monotonic", None)
    if start is None:
        return 0.0
    return max(time.monotonic() - start, 0.0)


# --- endpoints ---------------------------------------------------------------


@router.get("/info", response_model=SystemInfoRead)
def get_system_info(request: Request, session: Session = Depends(get_session)) -> SystemInfoRead:
    """Return the About panel's environment/runtime facts.

    ``ffmpeg_version`` is read from the cached ``app.state.ffmpeg_version``
    (probed once at startup -- see :func:`collapsarr.main.create_app`'s
    lifespan) rather than re-invoked per request; ``python_version``/``os``
    are cheap stdlib reads taken fresh via the injectable
    :class:`~collapsarr.system.probe.SystemProbe` on
    ``app.state.system_probe``. ``db_engine`` reports whichever SQLAlchemy
    dialect the app's own engine is actually configured with
    (``engine.dialect.name`` -- ``"sqlite"``, ``"postgresql"``, etc.), never
    assumed. ``disk`` reuses the disk-space health check's own probe
    (:func:`~collapsarr.health.disk_space.get_disk_usage`), including its
    ``app.state.disk_usage`` test-injection seam, rather than a second,
    independent filesystem read.
    """
    settings: Settings = request.app.state.settings
    probe: SystemProbe = request.app.state.system_probe
    usage = get_disk_usage(settings.data_dir, request.app.state.disk_usage)
    engine = request.app.state.engine

    return SystemInfoRead(
        app_version=__version__,
        install_method=_install_method(),
        python_version=probe.python_version(),
        ffmpeg_version=request.app.state.ffmpeg_version,
        os=probe.os_platform(),
        db_engine=engine.dialect.name,
        db_schema_revision=_db_schema_revision(session),
        data_dir=settings.data_dir,
        database_path=settings.database_path,
        uptime_seconds=_uptime_seconds(request),
        timezone=_current_timezone(),
        disk=DiskUsageRead(free_bytes=usage.free, total_bytes=usage.total),
    )


__all__ = ["DiskUsageRead", "SystemInfoRead", "router"]
