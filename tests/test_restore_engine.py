"""Boot-time staged swap engine (COL-70).

Covers the boot-side consumer of a restore marker: on startup, before the engine
connects and before ``upgrade_to_head``, a marked restore takes a safety backup
of the current database, swaps the staged file into place, clears the marker,
and lets the schema upgrade forward-migrate an older restored DB. The defensive
path (missing / non-SQLite staged file, or a non-file database) aborts the swap
and boots normally on the untouched current database, and no marker at all is a
clean no-op.

End-to-end coverage drives the real lifespan via the ``TestClient`` context, so
the swap runs exactly where it does in production.
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient

from collapsarr.backup.service import (
    ARCHIVE_MEMBER_NAME,
    BACKUP_RESTORE,
    BACKUP_UPDATE,
    backup_type_dir,
    list_backups,
)
from collapsarr.config import Settings
from collapsarr.main import create_app
from collapsarr.migrations import (
    BASELINE_REVISION,
    build_alembic_config,
    upgrade_to_head,
)
from collapsarr.restore.engine import apply_pending_restore
from collapsarr.restore.marker import (
    read_restore_marker,
    restore_marker_path,
    write_restore_marker,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _insert_instance(db_path: Path, name: str) -> None:
    """Insert one recognisable ``arr_instances`` row (a sentinel for identity).

    ``arr_instances`` exists at the baseline revision and at head, so a row
    written here survives a forward migration -- letting a test prove *which*
    database is live after a swap.
    """
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute(
            "INSERT INTO arr_instances "
            "(name, type, base_url, api_key, status, created_at, updated_at) "
            "VALUES (?, 'sonarr', 'http://example', 'k', 'unknown', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (name,),
        )
        connection.commit()
    finally:
        connection.close()


def _instance_names(db_path: Path) -> list[str]:
    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute("SELECT name FROM arr_instances ORDER BY name")
        return [row[0] for row in rows]
    finally:
        connection.close()


def _db_revision(db_path: Path) -> str | None:
    connection = sqlite3.connect(str(db_path))
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        return None if row is None else str(row[0])
    finally:
        connection.close()


def _head_revision(settings: Settings) -> str | None:
    return ScriptDirectory.from_config(build_alembic_config(settings)).get_current_head()


def _build_staged_db(
    staged_path: Path, build_data_dir: Path, *, name: str, revision: str = "head"
) -> None:
    """Build a self-contained staged SQLite DB carrying a sentinel ``name`` row.

    ``revision="head"`` builds a current-schema database; passing an explicit
    older revision (e.g. the baseline) builds an older DB, to exercise the
    forward-migrate-after-swap path. ``build_data_dir`` isolates any incidental
    backup/data writes away from the app's own data dir.
    """
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    build_settings = Settings(database_path=str(staged_path), data_dir=str(build_data_dir))
    if revision == "head":
        upgrade_to_head(build_settings)
    else:
        command.upgrade(build_alembic_config(build_settings), revision)
    _insert_instance(staged_path, name)


def _current_db_with(settings: Settings, name: str) -> None:
    """Materialise the app's current database at head with a sentinel row."""
    upgrade_to_head(settings)
    _insert_instance(Path(settings.database_path), name)


# --------------------------------------------------------------------------- #
# No marker -> clean no-op
# --------------------------------------------------------------------------- #
def test_no_marker_returns_none_and_writes_nothing(settings: Settings) -> None:
    assert apply_pending_restore(settings) is None
    assert not restore_marker_path(settings).exists()
    assert list_backups(settings) == []


def test_no_marker_boots_normally(client: TestClient, settings: Settings) -> None:
    """A normal boot (no marker) serves and takes no safety backup."""
    assert client.get("/health").status_code == 200
    assert list_backups(settings) == []


# --------------------------------------------------------------------------- #
# Valid staged file -> safety backup + swap + serve
# --------------------------------------------------------------------------- #
def test_restore_swaps_in_staged_db_and_serves(settings: Settings, tmp_path: Path) -> None:
    _current_db_with(settings, "CURRENT")
    staged = tmp_path / "staged" / "staged.db"
    _build_staged_db(staged, tmp_path / "staged_build", name="STAGED")
    write_restore_marker(settings, staged)

    app = create_app(settings=settings)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        # The live database now serves the restored (STAGED) data, not CURRENT.
        assert _instance_names(Path(settings.database_path)) == ["STAGED"]

    # Marker cleared and staged file consumed.
    assert not restore_marker_path(settings).exists()
    assert not staged.exists()


def test_safety_backup_lands_as_listable_restore_backup(settings: Settings, tmp_path: Path) -> None:
    """The pre-swap safety backup is a listable ``restore`` backup of the CURRENT DB."""
    _current_db_with(settings, "CURRENT")
    staged = tmp_path / "staged" / "staged.db"
    _build_staged_db(staged, tmp_path / "staged_build", name="STAGED")
    write_restore_marker(settings, staged)

    app = create_app(settings=settings)
    with TestClient(app):
        pass

    # Staged DB is at head, so no pre-migration backup is added: exactly the one
    # safety backup exists, under the dedicated ``restore`` type.
    backups = list_backups(settings)
    assert len(backups) == 1
    assert backups[0].type == BACKUP_RESTORE

    # The safety backup holds a byte-for-byte copy of the pre-restore CURRENT DB.
    archive_path = backup_type_dir(settings, BACKUP_RESTORE) / backups[0].name
    verify_db = tmp_path / "safety_verify.db"
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [ARCHIVE_MEMBER_NAME]
        verify_db.write_bytes(archive.read(ARCHIVE_MEMBER_NAME))
    assert _instance_names(verify_db) == ["CURRENT"]


def test_older_restored_db_is_migrated_forward(settings: Settings, tmp_path: Path) -> None:
    """A staged DB at an older revision is migrated up to head after the swap."""
    _current_db_with(settings, "CURRENT")
    staged = tmp_path / "staged" / "staged.db"
    _build_staged_db(
        staged, tmp_path / "staged_build", name="STAGED", revision=BASELINE_REVISION
    )
    write_restore_marker(settings, staged)
    assert _db_revision(staged) == BASELINE_REVISION  # older than head before boot

    app = create_app(settings=settings)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    live_db = Path(settings.database_path)
    # Restored data is served AND forward-migrated to head.
    assert _instance_names(live_db) == ["STAGED"]
    assert _db_revision(live_db) == _head_revision(settings)

    # Two backups survive in separate type dirs (a same-second name would collide
    # in one dir): the pre-swap ``restore`` safety backup (of CURRENT) and the
    # ``update`` pre-migration backup of the swapped-in older DB.
    by_type = {b.type for b in list_backups(settings)}
    assert BACKUP_RESTORE in by_type
    assert BACKUP_UPDATE in by_type

    # The safety backup still holds the pre-restore CURRENT DB (not overwritten).
    restore_backups = [b for b in list_backups(settings) if b.type == BACKUP_RESTORE]
    assert len(restore_backups) == 1
    archive_path = backup_type_dir(settings, BACKUP_RESTORE) / restore_backups[0].name
    verify_db = tmp_path / "older_safety_verify.db"
    with zipfile.ZipFile(archive_path) as archive:
        verify_db.write_bytes(archive.read(ARCHIVE_MEMBER_NAME))
    assert _instance_names(verify_db) == ["CURRENT"]


# --------------------------------------------------------------------------- #
# Defensive aborts -> current DB untouched, marker cleared, boots normally
# --------------------------------------------------------------------------- #
def test_missing_staged_file_aborts_and_leaves_current_untouched(
    settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _current_db_with(settings, "CURRENT")
    write_restore_marker(settings, tmp_path / "does_not_exist.db")

    app = create_app(settings=settings)
    with caplog.at_level("ERROR"):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert _instance_names(Path(settings.database_path)) == ["CURRENT"]

    assert "RESTORE ABORTED" in caplog.text
    assert not restore_marker_path(settings).exists()
    # Nothing was swapped, so no safety backup was taken.
    assert list_backups(settings) == []


def test_non_sqlite_staged_file_aborts(
    settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _current_db_with(settings, "CURRENT")
    staged = tmp_path / "not-a-db.txt"
    staged.write_text("this is definitely not a SQLite database")
    write_restore_marker(settings, staged)

    app = create_app(settings=settings)
    with caplog.at_level("ERROR"):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert _instance_names(Path(settings.database_path)) == ["CURRENT"]

    assert "RESTORE ABORTED" in caplog.text
    assert not restore_marker_path(settings).exists()
    assert staged.exists()  # a rejected staged file is not consumed
    assert list_backups(settings) == []


def test_malformed_marker_aborts_and_is_cleared(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    _current_db_with(settings, "CURRENT")
    marker_path = restore_marker_path(settings)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text("}{ not json")

    with caplog.at_level("ERROR"):
        outcome = apply_pending_restore(settings)

    assert outcome is not None and outcome.applied is False
    assert "RESTORE ABORTED" in caplog.text
    assert not marker_path.exists()
    assert _instance_names(Path(settings.database_path)) == ["CURRENT"]
    assert list_backups(settings) == []


def test_non_file_database_aborts(settings: Settings, tmp_path: Path) -> None:
    """A valid staged file but a non-file (e.g. Postgres) database aborts the swap."""
    non_file = settings.model_copy(
        update={"database_url": "postgresql://user:pass@localhost/collapsarr"}
    )
    staged = tmp_path / "staged.db"
    _build_staged_db(staged, tmp_path / "staged_build", name="STAGED")
    write_restore_marker(non_file, staged)

    outcome = apply_pending_restore(non_file)

    assert outcome is not None and outcome.applied is False
    assert outcome.reason is not None and "file-based" in outcome.reason
    assert not restore_marker_path(non_file).exists()
    assert staged.exists()  # untouched


# --------------------------------------------------------------------------- #
# Marker round-trip
# --------------------------------------------------------------------------- #
def test_marker_round_trip(settings: Settings, tmp_path: Path) -> None:
    staged = tmp_path / "staged.db"
    write_restore_marker(settings, staged)
    marker = read_restore_marker(settings)
    assert marker is not None
    assert marker.staged_path == staged.resolve()


def test_read_marker_absent_returns_none(settings: Settings) -> None:
    assert read_restore_marker(settings) is None
