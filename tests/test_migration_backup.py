"""Backup-before-migrate with retention (COL-60).

Covers the pre-migration safety net added to ``upgrade_to_head``: a plain
SQLite file copy taken before any pending migration mutates the database,
retained up to :data:`~collapsarr.migrations.BACKUP_RETENTION_COUNT` copies,
skipped entirely when nothing is pending or when the configured backend isn't
a file-based SQLite database, and exercised end-to-end by a forced-failure
test asserting the fail-fast contract (backup exists, DB left at the last
good revision, boot never reaches serving).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient

import collapsarr.migrations as migrations_module
from collapsarr.config import Settings
from collapsarr.database import Base, create_engine_from_settings
from collapsarr.main import create_app
from collapsarr.migrations import (
    BACKUP_RETENTION_COUNT,
    BASELINE_REVISION,
    _backup_before_migration,
    _prune_old_backups,
    _sqlite_file_path,
    build_alembic_config,
    upgrade_to_head,
)


def _backups_dir(settings: Settings) -> Path:
    return Path(settings.data_dir).expanduser() / "backups"


def _db_file_name(settings: Settings) -> str:
    return Path(settings.database_path).name


# --------------------------------------------------------------------------- #
# Backup-when-pending / no churn on a normal boot
# --------------------------------------------------------------------------- #
def test_no_backup_on_fresh_install(settings: Settings) -> None:
    """A brand-new install (no pre-existing file) writes no backup.

    Migrations are technically "pending" from an empty database, but there is
    no prior data on disk to protect, so this must not create
    ``data_dir/backups`` at all.
    """
    upgrade_to_head(settings)

    assert not _backups_dir(settings).exists()


def test_no_backup_on_up_to_date_boot(settings: Settings) -> None:
    """A second boot against an already-current DB takes no backup either."""
    upgrade_to_head(settings)  # fresh install -> head, no backup (see above)
    upgrade_to_head(settings)  # already current -> no-op, still no backup

    assert not _backups_dir(settings).exists()


def test_backup_written_when_adopting_existing_unversioned_db(settings: Settings) -> None:
    """An adopted (unversioned, populated) DB is backed up before the stamp.

    Covers the COL-59 interaction: the backup must cover the pre-stamp state,
    so it's taken before the baseline stamp mutates the database at all.
    """
    engine = create_engine_from_settings(settings)
    Base.metadata.create_all(engine)  # create_all-era install: populated, unversioned
    engine.dispose()

    upgrade_to_head(settings)

    backups = list(_backups_dir(settings).glob(f"{_db_file_name(settings)}.pre-*.bak"))
    assert len(backups) == 1
    # Unversioned pre-migration state has no revision string -- labelled explicitly.
    assert "unversioned" in backups[0].name
    assert backups[0].stat().st_size > 0


def test_backup_filename_encodes_revision_and_timestamp(settings: Settings) -> None:
    """An incremental migration (already-versioned DB) names the backup after
    the pre-migration revision, plus a timestamp suffix."""
    # Stamp+build the DB at the baseline only (a versioned, but not-yet-head,
    # install) so upgrading to head is a plain incremental migration.
    config = build_alembic_config(settings)
    command.upgrade(config, BASELINE_REVISION)

    upgrade_to_head(settings)

    backups = list(_backups_dir(settings).glob(f"{_db_file_name(settings)}.pre-*.bak"))
    assert len(backups) == 1
    name = backups[0].name
    prefix = f"{_db_file_name(settings)}.pre-{BASELINE_REVISION}-"
    assert name.startswith(prefix)
    timestamp = name.removeprefix(prefix).removesuffix(".bak")
    assert timestamp.isdigit()
    assert len(timestamp) == 20  # %Y%m%d%H%M%S%f


def test_backup_path_is_logged(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The backup path is logged when a backup is actually taken."""
    engine = create_engine_from_settings(settings)
    Base.metadata.create_all(engine)
    engine.dispose()

    with caplog.at_level("INFO"):
        upgrade_to_head(settings)

    backups = list(_backups_dir(settings).glob(f"{_db_file_name(settings)}.pre-*.bak"))
    assert len(backups) == 1
    assert str(backups[0]) in caplog.text


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
def test_retention_keeps_only_last_five_backups(settings: Settings) -> None:
    """Pruning keeps only the most recent :data:`BACKUP_RETENTION_COUNT`."""
    backups_dir = _backups_dir(settings)
    backups_dir.mkdir(parents=True)
    db_name = _db_file_name(settings)

    # Six pre-existing "stale" backups with strictly increasing, deliberately
    # old mtimes, so a new real backup should push the oldest two out.
    base_epoch = 1_700_000_000  # a fixed point well in the past
    stale_names = []
    for i in range(6):
        stale = backups_dir / f"{db_name}.pre-stale{i}-{i:020d}.bak"
        stale.write_bytes(b"x")
        os.utime(stale, (base_epoch + i, base_epoch + i))
        stale_names.append(stale.name)

    # Trigger one real backup (adoption path).
    engine = create_engine_from_settings(settings)
    Base.metadata.create_all(engine)
    engine.dispose()
    upgrade_to_head(settings)

    remaining = {p.name for p in backups_dir.glob(f"{db_name}.pre-*.bak")}
    assert len(remaining) == BACKUP_RETENTION_COUNT
    # The two oldest stale backups were pruned.
    assert stale_names[0] not in remaining
    assert stale_names[1] not in remaining
    # The four newest stale backups plus the just-written real one remain.
    for name in stale_names[2:]:
        assert name in remaining
    assert any("unversioned" in name for name in remaining)


def test_retention_grows_to_five_then_caps_on_incremental_accrual(
    settings: Settings,
) -> None:
    """Backups accrued one at a time grow 1..5 then cap at 5 (oldest pruned).

    This models the realistic path -- one backup added per pending migration --
    where each prune sees ``len(backups) <= BACKUP_RETENTION_COUNT`` until the
    sixth. It is the regression guard for the negative-slice bug: with the
    buggy ``backups[:len-5]`` pruning, retention would cap at 2 (each prune
    below the limit sliced from the front and deleted a backup that should be
    kept) instead of growing to 5.
    """
    backups_dir = _backups_dir(settings)
    backups_dir.mkdir(parents=True)
    db_name = _db_file_name(settings)
    base_epoch = 1_700_000_000  # a fixed point well in the past

    expected_counts = [1, 2, 3, 4, 5, 5, 5]  # 7 accruals: grows to 5, then caps
    for i, expected in enumerate(expected_counts):
        # One new backup with a strictly-increasing mtime, then prune -- exactly
        # what _backup_before_migration does per pending migration.
        new_backup = backups_dir / f"{db_name}.pre-rev{i}-{i:020d}.bak"
        new_backup.write_bytes(b"x")
        os.utime(new_backup, (base_epoch + i, base_epoch + i))

        _prune_old_backups(backups_dir, db_name)

        remaining = sorted(
            backups_dir.glob(f"{db_name}.pre-*.bak"),
            key=lambda p: p.stat().st_mtime,
        )
        assert len(remaining) == expected, f"after accrual #{i + 1}"
        # Retention keeps the *newest*: the just-written backup is always present.
        assert new_backup.name in {p.name for p in remaining}

    # After capping, the survivors are the five most-recent accruals (rev2..rev6).
    survivors = {p.name for p in backups_dir.glob(f"{db_name}.pre-*.bak")}
    assert survivors == {f"{db_name}.pre-rev{i}-{i:020d}.bak" for i in range(2, 7)}


# --------------------------------------------------------------------------- #
# Non-file database_url
# --------------------------------------------------------------------------- #
def test_sqlite_file_path_is_none_for_non_sqlite_url(settings: Settings) -> None:
    non_file_settings = settings.model_copy(
        update={"database_url": "postgresql://user:pass@localhost/collapsarr"}
    )
    assert _sqlite_file_path(non_file_settings) is None


def test_sqlite_file_path_is_none_for_memory_sentinel(settings: Settings) -> None:
    memory_by_path = settings.model_copy(update={"database_path": ":memory:"})
    assert _sqlite_file_path(memory_by_path) is None

    memory_by_url = settings.model_copy(update={"database_url": "sqlite:///:memory:"})
    assert _sqlite_file_path(memory_by_url) is None


def test_non_file_database_url_skips_backup_and_logs(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-file ``database_url`` skips the backup with a log line."""
    non_file_settings = settings.model_copy(
        update={"database_url": "postgresql://user:pass@localhost/collapsarr"}
    )

    with caplog.at_level("INFO"):
        _backup_before_migration(non_file_settings, None, False, None)

    assert "database_url does not point at a file-based SQLite database" in caplog.text
    assert not _backups_dir(non_file_settings).exists()


# --------------------------------------------------------------------------- #
# Fail-fast: forced migration failure
# --------------------------------------------------------------------------- #
_GOOD_REVISION = "col60testgoodrev"
_BAD_REVISION = "col60testbadrev"

_ENV_PY = '''\
from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
'''

_GOOD_REVISION_PY = f'''\
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "{_GOOD_REVISION}"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("col60_good_table", sa.Column("id", sa.Integer(), primary_key=True))


def downgrade() -> None:
    op.drop_table("col60_good_table")
'''

_BAD_REVISION_PY = f'''\
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "{_BAD_REVISION}"
down_revision = "{_GOOD_REVISION}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("col60_bad_table", sa.Column("id", sa.Integer(), primary_key=True))
    raise RuntimeError("simulated migration failure (COL-60 fail-fast test)")


def downgrade() -> None:
    raise NotImplementedError
'''


def _write_fail_chain(script_dir: Path) -> None:
    """Build a throwaway two-revision Alembic chain: a good revision, then one
    that partially applies (creates a table) and then raises.

    Isolated under ``tmp_path`` so it never touches the real
    ``collapsarr/migrations`` package -- ``MIGRATIONS_DIR`` is monkeypatched
    to point here for the duration of the test.
    """
    versions_dir = script_dir / "versions"
    versions_dir.mkdir(parents=True)
    (script_dir / "env.py").write_text(_ENV_PY)
    (versions_dir / "a_good.py").write_text(_GOOD_REVISION_PY)
    (versions_dir / "b_bad.py").write_text(_BAD_REVISION_PY)


def test_fail_fast_backs_up_keeps_last_good_revision_and_never_serves(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Forced migration failure: backup exists, DB at last-good revision, and
    the app never proceeds to serving (COL-60 fail-fast assertion test).

    Uses a throwaway two-revision chain (good -> bad) swapped in for
    ``MIGRATIONS_DIR`` so the failure is a *real* Alembic/SQLite transaction
    failure, not a mocked one -- this is what actually established (see this
    module's docstring / manual verification) that Alembic's version
    bookkeeping rolls back on SQLite even though the failed revision's own
    partial DDL can persist, which is exactly why the pre-migration backup is
    the real recovery path.

    (a) and (b) are asserted directly against ``upgrade_to_head``; (c) is
    asserted separately against a *fresh* database at the same last-good
    revision, because retrying ``upgrade_to_head`` against the same
    already-failed database would hit the bad revision's leftover partial DDL
    (e.g. "table already exists") instead of the simulated failure -- itself a
    demonstration of why the backup, not a retry, is the intended recovery.
    """
    script_dir = tmp_path / "col60_fail_chain"
    _write_fail_chain(script_dir)
    monkeypatch.setattr(migrations_module, "MIGRATIONS_DIR", script_dir)

    # Bring the DB to the "last good" revision first -- models an already
    # deployed install about to take an update that ships a broken migration.
    config = build_alembic_config(settings)
    command.upgrade(config, _GOOD_REVISION)

    with caplog.at_level("INFO"):
        with pytest.raises(RuntimeError, match="simulated migration failure"):
            upgrade_to_head(settings)

    # (a) the pre-migration backup exists, labelled with the last-good revision.
    backups = list(_backups_dir(settings).glob(f"{_db_file_name(settings)}.pre-*.bak"))
    assert len(backups) == 1
    assert _GOOD_REVISION in backups[0].name
    assert "Wrote pre-migration backup to" in caplog.text

    # (b) the DB is left at the last good revision (transactional rollback of
    # Alembic's version bookkeeping).
    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            revision = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    assert revision == _GOOD_REVISION

    # (c) boot does not proceed to serving: the lifespan runs upgrade_to_head
    # before anything else, so the same failure aborts entering the
    # TestClient context -- the app never yields control to serve requests.
    # Uses an independent, freshly-stamped database (see docstring) so this
    # is the failing migration's *first* attempt, not a retry against
    # already-partially-migrated leftovers.
    serving_dir = tmp_path / "serving"
    serving_dir.mkdir()
    serving_settings = Settings(
        database_path=str(serving_dir / "collapsarr.db"),
        data_dir=str(serving_dir),
    )
    command.upgrade(build_alembic_config(serving_settings), _GOOD_REVISION)

    app = create_app(settings=serving_settings)
    with pytest.raises(RuntimeError, match="simulated migration failure"):
        with TestClient(app):
            pytest.fail("app must not proceed to serving after a failed migration")
