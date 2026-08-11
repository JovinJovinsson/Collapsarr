"""Existing-database adoption: stamp-if-populated + reconcile-indexes (COL-59).

Two guarantees for upgrading a ``create_all``-era install (all tables, but no
``alembic_version`` and possibly missing indexes the baseline declares):

* **Golden-snapshot fidelity** -- ``upgrade head`` on an empty DB reproduces a
  committed ``sqlite_master`` snapshot captured from a real current-release
  database (``tests/fixtures/golden_schema_snapshot.json``). This is what makes
  stamping honest: the migration-built schema provably equals what unversioned
  installs already have on disk, so adopting them at baseline alters nothing.
  Deliberately *not* a ``create_all``-vs-``upgrade`` diff (both derive from the
  same ``Base.metadata`` -- circular); the snapshot is a frozen, independent
  artifact. **Throwaway guard**: retire this test and its fixture once no
  unversioned databases remain in the wild.

* **Stamp adoption + index heal** -- a populated-but-unversioned DB is stamped
  at baseline, the reconcile-indexes delta runs, the DB ends at head, existing
  rows survive, and previously-missing indexes are recreated.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from alembic import command
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from collapsarr.config import Settings
from collapsarr.database import Base, create_engine_from_settings
from collapsarr.migrations import (
    BASELINE_REVISION,
    SENTINEL_TABLE,
    build_alembic_config,
    upgrade_to_head,
)

GOLDEN_SNAPSHOT = Path(__file__).parent / "fixtures" / "golden_schema_snapshot.json"

#: The chain revision the golden snapshot was captured at -- COL-59's
#: reconcile-indexes revision, the last one before any *real* schema-adding
#: migration shipped (COL-66 onward). Pinned rather than "head" so the
#: fidelity test below keeps meaning what it always meant as the chain grows:
#: the snapshot is a frozen stand-in for a real pre-Alembic release's disk
#: schema, which by definition can never gain columns a later migration adds
#: (those installs adopt them via the normal upgrade path instead -- see
#: ``test_startup_adopts_unversioned_db_and_heals_indexes`` below). Comparing
#: against a moving "head" would make this test fail on every future
#: schema-adding migration, which is not the drift it exists to catch.
GOLDEN_SNAPSHOT_REVISION = "2bd1b849232b"

# The nine SQLAlchemy-declared indexes the baseline creates (name, unique?).
BASELINE_INDEXES = {
    "ix_arr_instances_name": False,
    "ix_arr_instances_type": False,
    "ix_job_history_file_path": False,
    "ix_job_history_job_id": True,
    "ix_job_history_status": False,
    "ix_tracked_media_files_file_path": True,
    "ix_remote_path_mappings_instance_id": False,
    "ix_tracked_media_target_status_media_id": False,
    "ix_tracked_media_target_status_status": False,
}


# --------------------------------------------------------------------------- #
# Schema normalization: compare two sqlite_master dumps for *semantic* equality,
# tolerating cosmetic differences between how create_all and Alembic's batch
# renderer emit DDL (whitespace, and the order of table-level constraints).
# --------------------------------------------------------------------------- #
_CONSTRAINT_RE = re.compile(r"^(PRIMARY KEY|FOREIGN KEY|UNIQUE|CHECK|CONSTRAINT)\b", re.I)


def _split_top_level(body: str) -> list[str]:
    """Split a parenthesised clause list on top-level commas."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        parts.append("".join(current).strip())
    return parts


def _normalize(sql: str) -> str:
    """Canonicalise a CREATE statement: collapse whitespace; sort table-level
    constraints (column order preserved). Index DDL is returned whitespace-only
    normalised."""
    collapsed = re.sub(r"\s+", " ", sql).strip()
    match = re.match(r"(.*?)\((.*)\)\s*$", collapsed, re.S)
    if not match or not match.group(1).strip().upper().startswith("CREATE TABLE"):
        return collapsed
    head, body = match.group(1).strip(), match.group(2)
    clauses = _split_top_level(body)
    columns = [c for c in clauses if not _CONSTRAINT_RE.match(c)]
    constraints = sorted(c for c in clauses if _CONSTRAINT_RE.match(c))
    return f"{head} ( {', '.join(columns + constraints)} )"


def _live_schema(settings: Settings) -> dict[tuple[str, str], str]:
    """Normalised sqlite_master of the live DB, keyed by (type, name).

    Excludes SQLite internals and Alembic's own ``alembic_version`` bookkeeping
    table (absent from a create_all-era DB, so not part of the fidelity claim).
    """
    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' AND name != 'alembic_version' "
                    "AND sql IS NOT NULL"
                )
            ).fetchall()
    finally:
        engine.dispose()
    return {(row[0], row[1]): _normalize(row[2]) for row in rows}


# --------------------------------------------------------------------------- #
# Golden-snapshot fidelity
# --------------------------------------------------------------------------- #
def test_upgrade_head_reproduces_golden_release_snapshot(settings: Settings) -> None:
    """`upgrade` to :data:`GOLDEN_SNAPSHOT_REVISION` on an empty DB matches the
    committed real-release schema.

    Deliberately upgrades to the pinned revision, not "head": see
    :data:`GOLDEN_SNAPSHOT_REVISION` for why comparing against a moving head
    would make this fail on every later schema-adding migration.
    """
    golden = json.loads(GOLDEN_SNAPSHOT.read_text())
    expected = {
        (obj["type"], obj["name"]): _normalize(obj["sql"]) for obj in golden["objects"]
    }

    config = build_alembic_config(settings)
    command.upgrade(config, GOLDEN_SNAPSHOT_REVISION)
    actual = _live_schema(settings)

    assert actual == expected


def test_golden_snapshot_is_documented_as_throwaway() -> None:
    """The fixture self-documents as a throwaway guard to retire later."""
    golden = json.loads(GOLDEN_SNAPSHOT.read_text())
    assert "THROWAWAY" in golden["_comment"].upper()


# --------------------------------------------------------------------------- #
# Empty / already-versioned behaviour
# --------------------------------------------------------------------------- #
def test_empty_database_is_not_stamped_and_runs_full_chain(settings: Settings) -> None:
    """A truly empty DB (no sentinel) builds from base up to head."""
    # Precondition: no database objects at all -> sentinel absent.
    engine = create_engine_from_settings(settings)
    try:
        assert not inspect(engine).has_table(SENTINEL_TABLE)
    finally:
        engine.dispose()

    upgrade_to_head(settings)

    config = build_alembic_config(settings)
    head = ScriptDirectory.from_config(config).get_current_head()
    engine = create_engine_from_settings(settings)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        for table in Base.metadata.sorted_tables:
            assert table.name in table_names
        with engine.connect() as connection:
            revision = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    # Reached head via the full chain (head is past baseline: the reconcile delta).
    assert revision == head
    assert head != BASELINE_REVISION


def test_already_versioned_database_runs_only_pending_deltas(settings: Settings) -> None:
    """A DB already at head is a no-op; re-running never rebuilds or errors."""
    upgrade_to_head(settings)  # fresh install -> head
    schema_before = _live_schema(settings)

    upgrade_to_head(settings)  # second run: already versioned, nothing pending
    schema_after = _live_schema(settings)

    assert schema_after == schema_before


# --------------------------------------------------------------------------- #
# Stamp adoption + index heal
# --------------------------------------------------------------------------- #
#: Columns a *post-baseline* migration adds (COL-66's backup schedule knobs,
#: COL-79's disk-space thresholds, COL-101's Library-node bridge ids, COL-151's
#: Preferred Default Audio setting, COL-154's current-default-track snapshot,
#: COL-155's job-history ``kind``, COL-163's job-history ``priority``).
#: ``create_all`` below always builds the table from the live
#: ``Base.metadata`` -- i.e. with these columns already present -- so they are
#: dropped by raw DDL afterwards to de-evolve the stand-in back to what a real
#: pre-COL-66/pre-COL-79/pre-COL-101/pre-COL-151/pre-COL-154/pre-COL-155/pre-COL-163
#: create_all-era release actually had on disk. This mirrors the ``DROP
#: INDEX`` idiom just below for the same reason: the unversioned DB this
#: function fabricates predates every post-baseline delta, not just the
#: index-reconcile one.
POST_BASELINE_COLUMNS: tuple[tuple[str, str], ...] = (
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
    ("tracked_media_files", "instance_id"),
    ("tracked_media_files", "sonarr_episode_id"),
    ("tracked_media_files", "radarr_movie_id"),
    ("tracked_media_files", "current_default_language"),
    ("tracked_media_files", "current_default_channel_layout"),
    ("job_history", "kind"),
    ("job_history", "priority"),
)

#: Indexes a *post-baseline* migration adds on an *indexed* post-baseline
#: column (COL-101's three, plus COL-155's job-history ``kind`` and COL-163's
#: job-history ``priority`` -- see :data:`POST_BASELINE_COLUMNS` above; none
#: of the earlier post-baseline columns were indexed). Unlike
#: :data:`BASELINE_INDEXES` (which the baseline migration itself owns and the
#: reconcile-indexes delta heals independently of column adoption), these
#: only exist at all because ``create_all`` built the column they index -- so
#: they must be dropped *before* that column, mirroring the baseline's own
#: index-then-column drop order.
POST_BASELINE_INDEXES: tuple[str, ...] = (
    "ix_tracked_media_files_instance_id",
    "ix_tracked_media_files_sonarr_episode_id",
    "ix_tracked_media_files_radarr_movie_id",
    "ix_job_history_kind",
    "ix_job_history_priority",
)

#: Whole tables a *post-baseline* migration adds (COL-75's
#: ``health_check_state``, COL-80's ``health_write_probe``). ``create_all``
#: below builds them from the live ``Base.metadata``, so they are dropped
#: afterwards to de-evolve the stand-in back to a real pre-COL-75/pre-COL-80
#: create_all-era release -- exactly as :data:`POST_BASELINE_COLUMNS` does for
#: later-added columns -- so the adoption delta (not create_all) is what
#: creates them.
POST_BASELINE_TABLES: tuple[str, ...] = (
    "health_check_state",
    "health_write_probe",
    "update_check_state",
    "library_nodes",
)


def _build_populated_unversioned_db(settings: Settings) -> None:
    """Construct a create_all-era database: full schema, no ``alembic_version``,
    populated rows, and (raw DDL) the SQLAlchemy indexes dropped to simulate the
    retired ``ensure_schema`` gap (it could add columns but never indexes).

    ``create_all`` is the real pre-COL-56 build path, so this is a faithful
    stand-in for a database in the wild; the raw ``DROP INDEX`` / ``DROP
    COLUMN`` / ``INSERT`` shape the specific "populated but missing indexes
    and post-baseline columns, unversioned" precondition independently of the
    code under test.
    """
    engine = create_engine_from_settings(settings)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        # Drop every SQLAlchemy-declared index (older create_all-era DB).
        for index_name in BASELINE_INDEXES:
            connection.execute(text(f'DROP INDEX IF EXISTS "{index_name}"'))
        # Drop indexes on post-baseline columns *before* the columns
        # themselves (below) -- same reasoning as the baseline indexes above.
        for index_name in POST_BASELINE_INDEXES:
            connection.execute(text(f'DROP INDEX IF EXISTS "{index_name}"'))
        # Drop columns a post-baseline migration owns, so the adoption delta
        # (not create_all) is what adds them back -- same reasoning as the
        # dropped indexes above.
        #
        # ``tracked_media_files.instance_id`` is skipped by the plain-DDL loop
        # and dropped separately just below: it carries a FK to
        # ``arr_instances`` (created inline by ``create_all``, unlike the
        # migration's own explicitly-named one), and SQLite's native `ALTER
        # TABLE ... DROP COLUMN` refuses to drop a column that participates
        # in a FK constraint defined on the same table. The real migration
        # hits this identical limitation (see its module docstring) and works
        # around it with a ``batch_alter_table(..., recreate='always')``
        # table rebuild; the same approach is used here, via a bare
        # `Operations` bound to this connection (there is no active Alembic
        # migration context in this fixture).
        for table_name, column_name in POST_BASELINE_COLUMNS:
            if (table_name, column_name) == ("tracked_media_files", "instance_id"):
                continue
            connection.execute(
                text(f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"')
            )
        batch_ctx = MigrationContext.configure(connection)
        batch_ops = Operations(batch_ctx)
        with batch_ops.batch_alter_table(
            "tracked_media_files", recreate="always"
        ) as batch_op:
            batch_op.drop_column("instance_id")
        # Drop whole tables a post-baseline migration owns, so the adoption delta
        # (not create_all) is what creates them -- same reasoning as the columns.
        for table_name in POST_BASELINE_TABLES:
            connection.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
        # Populate the sentinel + a couple of indexed tables via raw DML.
        connection.execute(
            text(
                "INSERT INTO global_settings "
                "(id, api_key, enabled_targets, stereo_codec, surround_codec, "
                " concurrency_limit, ui_auth_enabled, auth_method, auth_required, "
                " created_at, updated_at) "
                "VALUES (1, 'existingkey', 'stereo', 'aac', 'ac3', 1, 0, "
                " 'forms', 'local_bypass', "
                " '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO job_history "
                "(id, job_id, file_path, status, created_at, updated_at) "
                "VALUES (1, 'job-abc', '/media/movie.mkv', 'succeeded', "
                " '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO tracked_media_files "
                "(id, file_path, created_at, updated_at) "
                "VALUES (1, '/media/movie.mkv', "
                " '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
            )
        )
    engine.dispose()


def _current_revision(settings: Settings) -> str | None:
    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _index_names(settings: Settings) -> set[str]:
    engine = create_engine_from_settings(settings)
    try:
        inspector = inspect(engine)
        names: set[str] = set()
        for table in Base.metadata.sorted_tables:
            if inspector.has_table(table.name):
                names |= {
                    str(idx["name"]) for idx in inspector.get_indexes(table.name)
                }
        return names
    finally:
        engine.dispose()


def test_unversioned_populated_db_precondition(settings: Settings) -> None:
    """Sanity-check the fixture: unversioned, populated, indexes absent."""
    _build_populated_unversioned_db(settings)

    assert _current_revision(settings) is None  # no alembic_version
    engine = create_engine_from_settings(settings)
    try:
        assert inspect(engine).has_table(SENTINEL_TABLE)  # populated sentinel
    finally:
        engine.dispose()
    assert _index_names(settings).isdisjoint(BASELINE_INDEXES)  # indexes missing


def test_startup_adopts_unversioned_db_and_heals_indexes(settings: Settings) -> None:
    """Unversioned populated DB -> stamped baseline -> head, data + indexes healed."""
    _build_populated_unversioned_db(settings)
    indexes_before = _index_names(settings)
    assert indexes_before.isdisjoint(BASELINE_INDEXES)  # heal target: all missing

    upgrade_to_head(settings)

    # Ended at head (baseline stamp + reconcile-indexes delta applied).
    config = build_alembic_config(settings)
    head = ScriptDirectory.from_config(config).get_current_head()
    assert _current_revision(settings) == head
    assert head != BASELINE_REVISION  # a real delta ran after the stamp

    # Every previously-missing index now exists, with correct uniqueness.
    engine = create_engine_from_settings(settings)
    try:
        inspector = inspect(engine)
        healed = {
            idx["name"]: idx["unique"]
            for table in Base.metadata.sorted_tables
            for idx in inspector.get_indexes(table.name)
        }
    finally:
        engine.dispose()
    for name, unique in BASELINE_INDEXES.items():
        assert name in healed, f"index {name} was not healed"
        assert bool(healed[name]) == unique, f"index {name} uniqueness mismatch"

    # Existing rows preserved (schema adopted, not rebuilt).
    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT api_key FROM global_settings WHERE id = 1")
            ).scalar_one() == "existingkey"
            assert connection.execute(
                text("SELECT job_id FROM job_history WHERE id = 1")
            ).scalar_one() == "job-abc"
            assert connection.execute(
                text("SELECT file_path FROM tracked_media_files WHERE id = 1")
            ).scalar_one() == "/media/movie.mkv"
    finally:
        engine.dispose()


def test_reconcile_indexes_is_idempotent_on_fresh_install(settings: Settings) -> None:
    """The reconcile-indexes migration is a no-op when indexes already exist."""
    upgrade_to_head(settings)  # fresh install: baseline built the indexes already
    indexes_after_first = _index_names(settings)
    assert BASELINE_INDEXES.keys() <= indexes_after_first

    # Re-running the whole routine (which re-evaluates the chain) changes nothing.
    upgrade_to_head(settings)
    assert _index_names(settings) == indexes_after_first
