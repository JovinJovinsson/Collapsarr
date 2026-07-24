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
import shutil
from datetime import UTC, datetime
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

#: Number of pre-migration backups retained under ``data_dir/backups`` (COL-60).
#: Hardcoded, not configurable -- the oldest backups beyond this count are
#: pruned every time a new one is written. See :func:`upgrade_to_head`.
BACKUP_RETENTION_COUNT = 5


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


def _prune_old_backups(backups_dir: Path, db_file_name: str) -> None:
    """Keep only the most recent :data:`BACKUP_RETENTION_COUNT` backups.

    Ordered by file modification time (not filename) so pruning stays correct
    regardless of how the revision label sorts lexicographically.
    """
    backups = sorted(
        backups_dir.glob(f"{db_file_name}.pre-*.bak"),
        key=lambda path: path.stat().st_mtime,
    )
    stale_count = len(backups) - BACKUP_RETENTION_COUNT
    for stale in backups[:stale_count]:
        stale.unlink(missing_ok=True)


def _backup_before_migration(
    settings: Settings,
    db_file: Path | None,
    db_file_existed: bool,
    pre_migration_revision: str | None,
) -> None:
    """Snapshot the SQLite file before a pending migration mutates it (COL-60).

    Called from :func:`upgrade_to_head` only once migrations are known to be
    pending (and before the adoption stamp or the upgrade itself runs, so it
    covers the pre-stamp state too). A plain file copy is safe here because
    startup runs before any session/connection writer opens.

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

    backups_dir = Path(settings.data_dir).expanduser() / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    revision_label = pre_migration_revision or "unversioned"
    backup_path = backups_dir / f"{db_file.name}.pre-{revision_label}-{timestamp}.bak"
    shutil.copy2(db_file, backup_path)
    logger.info("Wrote pre-migration backup to %s", backup_path)

    _prune_old_backups(backups_dir, db_file.name)


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

    Backup-before-migrate (COL-60): once *anything* is determined to be
    pending -- a stamp, an upgrade, or both -- the SQLite file is copied to
    ``data_dir/backups/<db file>.pre-<revision>-<timestamp>.bak`` before either
    mutates it (so the adoption stamp is covered too), and backups beyond the
    last :data:`BACKUP_RETENTION_COUNT` are pruned. Skipped for a non-file
    ``database_url`` (logged) and for a boot with nothing pending (no backup
    churn on a normal boot).

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
    pre_migration_revision = current_revision

    # Adopt an unversioned-but-populated (create_all-era) database: stamp the
    # baseline it already matches, then let the normal upgrade apply new deltas.
    # Guarded on current_revision being None, so it never re-stamps a versioned
    # DB — the stamp is idempotent by construction.
    adopting = current_revision is None and _has_sentinel_table(settings)

    if current_revision != head_revision:
        _backup_before_migration(settings, db_file, db_file_existed, pre_migration_revision)

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
    "BACKUP_RETENTION_COUNT",
    "BASELINE_REVISION",
    "MIGRATIONS_DIR",
    "SENTINEL_TABLE",
    "build_alembic_config",
    "upgrade_to_head",
]
