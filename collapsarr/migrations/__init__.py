"""Alembic migration package for Collapsarr (COL-57).

Ships *inside* the installed package so the migration environment (``env.py``),
the revision template (``script.py.mako``), and the versioned migrations under
``versions/`` are all present in the built wheel / Docker image — not just in a
source checkout. This module also builds the runtime Alembic
:class:`~alembic.config.Config` programmatically from :class:`Settings`, so the
app never reads an ``alembic.ini`` at runtime (the repo-root ``alembic.ini`` is
an authoring-only convenience for ``alembic revision --autogenerate`` /
``alembic history``).

SQLite is the only supported and tested backend; see ``README.md`` in this
directory for the migration-authoring workflow.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from collapsarr.config import Settings

logger = logging.getLogger(__name__)

#: Absolute path to this migrations directory. Doubles as Alembic's
#: ``script_location`` at runtime — resolved from ``__file__`` so it points at
#: the *installed* location inside the wheel, not a repo-relative path.
MIGRATIONS_DIR = Path(__file__).resolve().parent

#: Baseline revision (the schema a ``create_all``-era database already has). An
#: unversioned-but-populated DB is stamped here to adopt its existing schema
#: without rebuilding it; see :func:`upgrade_to_head`.
BASELINE_REVISION = "afde30c41b7b"

#: Sentinel table proving an unversioned database is a real, populated Collapsarr
#: install (not an empty file). Its presence is what distinguishes "adopt this
#: existing schema" from "build from base". ``global_settings`` is the singleton
#: settings row every install has.
SENTINEL_TABLE = "global_settings"


def build_alembic_config(settings: Settings) -> Config:
    """Build a runtime Alembic :class:`~alembic.config.Config` from ``settings``.

    No ``alembic.ini`` is read: ``script_location`` is pinned to this packaged
    directory and ``sqlalchemy.url`` comes from
    :attr:`Settings.sqlalchemy_url`. This is the Config the application hands to
    ``alembic.command.upgrade(config, "head")`` (see :func:`upgrade_to_head`).
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url)
    return config


def _current_revision(settings: Settings) -> str | None:
    """Return the revision the database is stamped at, or ``None`` if empty.

    A brand-new (fresh-install) database has no ``alembic_version`` row yet, so
    this reports ``None`` — the signal that the full chain from base needs to
    run.
    """
    # Reuse the app's engine builder so the data dir / SQLite parent exist and
    # the SQLite ``check_same_thread`` tweak is applied. Imported here (not at
    # module scope) to keep import order simple — ``database`` only depends on
    # ``config``, so there is no cycle.
    from collapsarr.database import create_engine_from_settings

    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _has_sentinel_table(settings: Settings) -> bool:
    """Return whether the sentinel table (:data:`SENTINEL_TABLE`) exists.

    Used only when the DB is unversioned (no ``alembic_version``): a populated
    install has this table (adopt at baseline), a truly empty file does not
    (build from base).
    """
    from collapsarr.database import create_engine_from_settings

    engine = create_engine_from_settings(settings)
    try:
        return inspect(engine).has_table(SENTINEL_TABLE)
    finally:
        engine.dispose()


def _sqlite_file_path(settings: Settings) -> Path | None:
    """Return the on-disk path of a file-based SQLite database, or ``None``.

    ``None`` covers both a non-SQLite ``database_url`` override (Postgres,
    etc.) and SQLite's own in-memory sentinel (``:memory:``) -- neither has a
    file on disk for the pre-migration backup (:func:`upgrade_to_head`) to
    copy. Mirrors the file/URL handling in
    :func:`collapsarr.database.create_engine_from_settings`.
    """
    if settings.database_url is None:
        if settings.database_path == ":memory:":
            return None
        return Path(settings.database_path).expanduser()

    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    _, _, path_part = url.partition("sqlite:///")
    if not path_part or path_part == ":memory:":
        return None
    return Path(path_part).expanduser()


def _backup_before_migration(
    settings: Settings,
    db_file: Path | None,
    db_file_existed: bool,
) -> None:
    """Snapshot the SQLite file before a pending migration mutates it (COL-69).

    Called from :func:`upgrade_to_head` only once migrations are known to be
    pending (and before the adoption stamp or the upgrade itself runs, so it
    covers the pre-stamp state too).

    Emits a **unified ``update``-type backup** through the shared backup service
    (:func:`collapsarr.backup.service.create_backup`): a zip under
    ``<data_dir>/backups/update/``, listable alongside the manual and scheduled
    backups and pruned by the one unified retention model -- an age-based window
    plus the ``update`` guaranteed-minimum floor (:data:`UPDATE_BACKUP_MIN_KEEP`,
    COL-68) -- not a private count-based scheme. This is COL-69's fold of the
    former standalone ``.bak`` backup into the single backup story.

    The one property the pre-migration path keeps that the manual/scheduled
    paths don't: it runs *before the app's engine connects*, so the service is
    handed :func:`~collapsarr.backup.service._raw_copy_snapshot` -- a
    byte-for-byte copy of the source file (the exact rollback artifact for a
    schema upgrade) instead of the live-database ``VACUUM INTO``.

    No-ops (with a log line) for a non-file ``database_url``. Also no-ops,
    silently, when the file doesn't exist yet -- a brand-new install has no
    prior data a migration could destroy, so there is nothing to protect and
    backing up an empty/absent file would just be churn on every fresh boot.
    """
    if db_file is None:
        logger.info(
            "Skipping pre-migration backup: database_url does not point at a "
            "file-based SQLite database"
        )
        return
    if not db_file_existed:
        return

    # Imported lazily: the backup service imports this module at import time
    # (for ``_sqlite_file_path``), so a module-level import here would be a
    # circular import.
    from collapsarr.backup.service import (
        BACKUP_UPDATE,
        _raw_copy_snapshot,
        create_backup,
    )
    from collapsarr.settings.models import DEFAULT_BACKUP_RETENTION_DAYS

    # Retention window is the schema default rather than the live
    # ``global_settings`` value: this runs before migrations apply, so that
    # column may not exist yet and reading it would create/commit the settings
    # row (a write) against a not-yet-migrated schema. The ``update``
    # guaranteed-minimum floor is what actually protects rollback points.
    create_backup(
        settings,
        BACKUP_UPDATE,
        retention_days=DEFAULT_BACKUP_RETENTION_DAYS,
        snapshot=_raw_copy_snapshot,
    )


def upgrade_to_head(settings: Settings) -> None:
    """Apply any pending Alembic migrations, bringing the schema up to head.

    This is the single schema-construction routine on the boot path (it replaced
    the retired ``init_db``/``create_all``/``ensure_schema``). Three cases:

    * **Fresh, empty database** — no ``alembic_version`` and no sentinel table:
      run the full migration chain from base.
    * **Unversioned but populated** (a ``create_all``-era install: no
      ``alembic_version`` but the sentinel :data:`SENTINEL_TABLE` exists):
      *stamp* the baseline revision to adopt the existing schema without
      rebuilding it, then upgrade to head so any post-baseline deltas (e.g. the
      reconcile-indexes heal) apply. This is COL-59's existing-DB adoption.
    * **Already versioned** — has ``alembic_version``: run only pending deltas
      (a no-op when already at head).

    Backup-before-migrate (COL-60, folded into the unified backup scheme by
    COL-69): once *anything* is determined to be pending -- a stamp, an upgrade,
    or both -- a unified ``update``-type backup zip is written under
    ``data_dir/backups/update/`` before either mutates the database (so the
    adoption stamp is covered too), and the unified retention model prunes it.
    Skipped for a non-file ``database_url`` (logged) and for a boot with nothing
    pending (no backup churn on a normal boot).

    Logging: emits the from→to revisions when a migration actually runs, and
    "schema is current" when there is nothing to do.

    Fail-fast: any error raised by Alembic propagates to the caller unchanged.
    Wired into the application lifespan, that means a failed migration aborts
    startup (non-zero exit) and the app never serves against a half-migrated
    schema. Alembic's SQLite version bookkeeping rolls back with it, so the DB
    reports the last-good revision even if a partially-applied DDL statement
    from the failed migration itself lingers -- the pre-migration backup taken
    above is the actual recovery path for that residue.
    """
    config = build_alembic_config(settings)
    head_revision = ScriptDirectory.from_config(config).get_current_head()

    # Snapshot whether the SQLite file already exists *before* anything below
    # touches it: ``_current_revision`` opens a connection, which SQLite
    # creates a (possibly empty) file for as a side effect, so this must be
    # read first to tell "pre-existing database" from "brand-new install".
    db_file = _sqlite_file_path(settings)
    db_file_existed = db_file is not None and db_file.exists()

    current_revision = _current_revision(settings)

    # Adopt an unversioned-but-populated (create_all-era) database: stamp the
    # baseline it already matches, then let the normal upgrade apply new deltas.
    # Guarded on current_revision being None, so it never re-stamps a versioned
    # DB — the stamp is idempotent by construction.
    adopting = current_revision is None and _has_sentinel_table(settings)

    if current_revision != head_revision:
        _backup_before_migration(settings, db_file, db_file_existed)

    if adopting:
        logger.info(
            "Adopting unversioned populated database: stamping baseline %s",
            BASELINE_REVISION,
        )
        command.stamp(config, BASELINE_REVISION)
        current_revision = BASELINE_REVISION

    if current_revision == head_revision:
        logger.info("Database schema is current (revision %s)", head_revision)
        return

    logger.info(
        "Migrating database schema from %s to %s",
        current_revision or "base (empty database)",
        head_revision,
    )
    command.upgrade(config, "head")


__all__ = [
    "BASELINE_REVISION",
    "MIGRATIONS_DIR",
    "SENTINEL_TABLE",
    "build_alembic_config",
    "upgrade_to_head",
]
