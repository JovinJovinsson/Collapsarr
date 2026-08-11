"""Pre-migration backup, folded into the unified backup scheme (COL-60 -> COL-69).

Covers the pre-migration safety net in ``upgrade_to_head``: instead of the old
standalone flat ``.bak`` copy with a private count-based retention, it now emits
a unified ``update``-type backup zip through the shared backup service -- landing
under ``<data_dir>/backups/update/``, listable alongside manual/scheduled
backups, and pruned by the one unified retention model (age-based window + the
``update`` guaranteed-minimum floor, COL-68).

The contract this suite guards is unchanged: the backup is taken before anything
mutates the database (covering the pre-stamp state), skipped entirely when
nothing is pending or the backend isn't a file-based SQLite database, and the
forced-failure test still asserts the fail-fast contract (backup exists, DB left
at the last good revision, boot never reaches serving). The one property the
pre-migration path keeps is that it runs *before the engine connects*, so the
artifact is a byte-for-byte raw copy of the source file (the exact rollback
point) rather than a live ``VACUUM INTO``.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest
from alembic import command
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import text

import collapsarr.migrations as migrations_module
from collapsarr.backup.service import (
    ARCHIVE_MEMBER_NAME,
    BACKUP_UPDATE,
    UPDATE_BACKUP_MIN_KEEP,
    BackupInfo,
    backup_type_dir,
    list_backups,
)
from collapsarr.config import Settings
from collapsarr.database import Base, create_engine_from_settings
from collapsarr.main import create_app
from collapsarr.migrations import (
    BASELINE_REVISION,
    _backup_before_migration,
    _sqlite_file_path,
    build_alembic_config,
    upgrade_to_head,
)

#: Columns a post-baseline migration adds (COL-66's backup schedule knobs,
#: COL-79's disk-space thresholds, COL-101's Library-node bridge ids, COL-151's
#: Preferred Default Audio setting, COL-154's current-default-track snapshot,
#: COL-155's job-history ``kind``, COL-163's job-history ``priority``) --
#: dropped after ``create_all`` below by :func:`_create_unversioned_db`,
#: mirroring the same de-evolving idiom in ``test_migration_adoption.py``.
#: Without this, ``create_all`` (which always builds from the *current*
#: ``Base.metadata``) leaves these columns already present, so the migration
#: that's supposed to add them fails.
_POST_BASELINE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("global_settings", "backup_interval_days"),
    ("global_settings", "backup_retention_days"),
    ("global_settings", "disk_space_warning_percent"),
    ("global_settings", "disk_space_error_percent"),
    ("global_settings", "update_channel"),
    ("global_settings", "default_tracked"),
    ("global_settings", "log_level"),
    ("global_settings", "default_audio_language"),
    ("global_settings", "default_audio_channel_tier"),
    ("global_settings", "auto_set_default_audio"),
    ("tracked_media_files", "sonarr_episode_id"),
    ("tracked_media_files", "radarr_movie_id"),
    ("tracked_media_files", "current_default_language"),
    ("tracked_media_files", "current_default_channel_layout"),
    ("job_history", "kind"),
    ("job_history", "priority"),
)

#: Indexes on the COL-101 ``tracked_media_files`` columns above -- SQLite's
#: plain ``ALTER TABLE ... DROP COLUMN`` refuses to drop an indexed column,
#: so these must go first (same reasoning as ``test_migration_adoption.py``'s
#: ``POST_BASELINE_INDEXES``). ``instance_id`` is handled separately in
#: :func:`_create_unversioned_db` (it also carries a FK, which needs a batch
#: table-rebuild rather than plain DDL).
_POST_BASELINE_INDEXES: tuple[str, ...] = (
    "ix_tracked_media_files_instance_id",
    "ix_tracked_media_files_sonarr_episode_id",
    "ix_tracked_media_files_radarr_movie_id",
    "ix_job_history_kind",
    "ix_job_history_priority",
)

#: Whole tables a post-baseline migration adds (COL-75's ``health_check_state``,
#: COL-80's ``health_write_probe``) -- dropped after ``create_all`` for the same
#: reason as ``_POST_BASELINE_COLUMNS``, so the migration that creates them does
#: not collide with a table ``create_all`` already built.
_POST_BASELINE_TABLES: tuple[str, ...] = (
    "health_check_state",
    "health_write_probe",
    "update_check_state",
    "library_nodes",
)


def _update_backups(settings: Settings) -> list[BackupInfo]:
    """Every finished ``update``-type backup, newest first (via the unified list)."""
    return [info for info in list_backups(settings) if info.type == BACKUP_UPDATE]


def _update_dir(settings: Settings) -> Path:
    return backup_type_dir(settings, BACKUP_UPDATE)


def _create_unversioned_db(settings: Settings) -> None:
    """Build a create_all-era, unversioned database (populated schema, no
    ``alembic_version``), de-evolved past any post-baseline column so the
    normal migration chain -- not ``create_all`` -- is what adds them."""
    engine = create_engine_from_settings(settings)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for index_name in _POST_BASELINE_INDEXES:
            connection.execute(text(f'DROP INDEX IF EXISTS "{index_name}"'))
        for table_name, column_name in _POST_BASELINE_COLUMNS:
            connection.execute(
                text(f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"')
            )
        # tracked_media_files.instance_id also carries a FK to arr_instances
        # (created inline by create_all); SQLite's plain ALTER TABLE ... DROP
        # COLUMN refuses a column that participates in a FK constraint
        # defined on the same table, so it needs a batch table-rebuild
        # instead (same limitation the real migration's downgrade() hits and
        # works around the same way -- see its module docstring, and
        # ``test_migration_adoption.py``'s identical fixture fix).
        batch_ctx = MigrationContext.configure(connection)
        batch_ops = Operations(batch_ctx)
        with batch_ops.batch_alter_table(
            "tracked_media_files", recreate="always"
        ) as batch_op:
            batch_op.drop_column("instance_id")
        for table_name in _POST_BASELINE_TABLES:
            connection.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
    engine.dispose()


# --------------------------------------------------------------------------- #
# Backup-when-pending / no churn on a normal boot
# --------------------------------------------------------------------------- #
def test_no_backup_on_fresh_install(settings: Settings) -> None:
    """A brand-new install (no pre-existing file) writes no backup.

    Migrations are technically "pending" from an empty database, but there is
    no prior data on disk to protect, so this must not write an ``update``
    backup -- and must not even create the ``update/`` directory (no churn).
    """
    upgrade_to_head(settings)

    assert _update_backups(settings) == []
    assert not _update_dir(settings).exists()


def test_no_backup_on_up_to_date_boot(settings: Settings) -> None:
    """A second boot against an already-current DB takes no backup either."""
    upgrade_to_head(settings)  # fresh install -> head, no backup (see above)
    upgrade_to_head(settings)  # already current -> no-op, still no backup

    assert _update_backups(settings) == []
    assert not _update_dir(settings).exists()


def test_backup_written_when_adopting_existing_unversioned_db(settings: Settings) -> None:
    """An adopted (unversioned, populated) DB gets a unified ``update`` zip
    before the stamp.

    Covers the COL-59 interaction: the backup must cover the pre-stamp state,
    so it's taken before the baseline stamp mutates the database at all. The
    artifact is the unified ``update``-type zip (COL-69), not a flat ``.bak``.
    """
    _create_unversioned_db(settings)  # create_all-era install: populated, unversioned

    upgrade_to_head(settings)

    backups = _update_backups(settings)
    assert len(backups) == 1
    info = backups[0]
    assert info.type == BACKUP_UPDATE
    assert info.size > 0
    # Lives under <data_dir>/backups/update/ and is the unified archive name.
    assert (_update_dir(settings) / info.name).is_file()
    assert info.name.startswith("collapsarr_backup_v")
    assert info.name.endswith(".zip")


def test_backup_is_unified_update_zip_containing_the_database(settings: Settings) -> None:
    """The pre-migration artifact is a real zip holding the database member.

    A versioned-but-not-head install (stamped at baseline) makes upgrading a
    plain incremental migration; the snapshot must be a byte-for-byte raw copy
    of the source file, packed as the unified archive member name so a restore
    step can find it deterministically.
    """
    config = build_alembic_config(settings)
    command.upgrade(config, BASELINE_REVISION)

    upgrade_to_head(settings)

    backups = _update_backups(settings)
    assert len(backups) == 1
    archive_path = _update_dir(settings) / backups[0].name
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [ARCHIVE_MEMBER_NAME]
        # The member is the raw source database (SQLite file header magic).
        assert archive.read(ARCHIVE_MEMBER_NAME).startswith(b"SQLite format 3\x00")


def test_backup_is_listable_alongside_other_backup_types(settings: Settings) -> None:
    """The pre-migration ``update`` backup shows up in the unified listing."""
    _create_unversioned_db(settings)

    upgrade_to_head(settings)

    all_backups = list_backups(settings)
    assert len(all_backups) == 1
    assert all_backups[0].type == BACKUP_UPDATE
    assert all_backups[0].id.startswith(f"{BACKUP_UPDATE}/")


def test_backup_path_is_logged(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The backup path is logged (by the shared service) when one is taken."""
    _create_unversioned_db(settings)

    with caplog.at_level("INFO"):
        upgrade_to_head(settings)

    backups = _update_backups(settings)
    assert len(backups) == 1
    archive_path = _update_dir(settings) / backups[0].name
    assert str(archive_path) in caplog.text
    assert "update backup" in caplog.text


# --------------------------------------------------------------------------- #
# Retention (unified model: age-based + update guaranteed-minimum, COL-68)
# --------------------------------------------------------------------------- #
def test_update_retention_prunes_stale_but_keeps_guaranteed_minimum(
    settings: Settings,
) -> None:
    """A pre-migration backup prunes ancient ``update`` archives past the window
    while keeping the :data:`UPDATE_BACKUP_MIN_KEEP` newest regardless of age.

    Retirement of COL-60's private count-based retention: pruning is now the
    shared, age-based :func:`~collapsarr.backup.service.prune_backups` with the
    ``update`` guaranteed-minimum floor. Several ancient ``update`` zips (far
    older than any retention window) are pre-seeded; taking one real
    pre-migration backup must prune all of them except the floor-count newest.
    """
    update_dir = _update_dir(settings)
    update_dir.mkdir(parents=True)

    # Ancient stale archives (well beyond the retention window), strictly
    # increasing mtimes so "newest" is unambiguous.
    base_epoch = 1_700_000_000  # a fixed point well in the past
    stale_names = []
    for i in range(4):
        stale = update_dir / f"collapsarr_backup_v0.0.0_stale_{i:02d}.zip"
        stale.write_bytes(b"x")
        os.utime(stale, (base_epoch + i, base_epoch + i))
        stale_names.append(stale.name)

    # Trigger one real pre-migration backup (adoption path).
    _create_unversioned_db(settings)
    upgrade_to_head(settings)

    remaining = {info.name for info in _update_backups(settings)}
    # Floor keeps exactly UPDATE_BACKUP_MIN_KEEP: the just-written real backup
    # plus the single newest stale one; the rest were pruned by age.
    assert len(remaining) == UPDATE_BACKUP_MIN_KEEP
    assert stale_names[-1] in remaining  # newest stale survives (floor slot)
    for name in stale_names[:-1]:
        assert name not in remaining  # older stale ones pruned by age


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
        _backup_before_migration(non_file_settings, None, False)

    assert "database_url does not point at a file-based SQLite database" in caplog.text
    assert _update_backups(non_file_settings) == []


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
    """Forced migration failure: a unified ``update`` backup exists, DB at
    last-good revision, and the app never proceeds to serving (fail-fast).

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

    # (a) the pre-migration backup exists as a unified ``update`` zip holding a
    # byte-for-byte copy of the last-good database (its raw SQLite bytes).
    backups = _update_backups(settings)
    assert len(backups) == 1
    archive_path = _update_dir(settings) / backups[0].name
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [ARCHIVE_MEMBER_NAME]
        assert archive.read(ARCHIVE_MEMBER_NAME).startswith(b"SQLite format 3\x00")
    assert "update backup" in caplog.text

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
