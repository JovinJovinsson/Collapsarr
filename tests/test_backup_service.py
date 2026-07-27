"""Service-level tests for the backup snapshot engine (COL-63).

Mirrors the shape of ``tests/test_migration_backup.py``: exercises
:func:`collapsarr.backup.service.create_backup` and friends directly against
the isolated ``settings`` fixture (a throwaway SQLite file under ``tmp_path``),
covering the happy path, the atomic/temp-then-rename failure contract, listing,
and the non-file-database no-op.
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest

import collapsarr.backup.service as backup_service
from collapsarr import __version__
from collapsarr.backup.service import (
    ARCHIVE_MEMBER_NAME,
    BACKUP_FILENAME_GLOB,
    BACKUP_MANUAL,
    BACKUP_SCHEDULED,
    BACKUP_TYPES,
    MINIMUM_BACKUP_KEEP,
    BackupNotFoundError,
    BackupRetentionFloorError,
    BackupUnavailableError,
    backup_type_dir,
    backups_root,
    create_backup,
    delete_backup,
    ensure_backup_dirs,
    is_backup_supported,
    list_backups,
    resolve_backup_path,
    resolve_sqlite_path,
)
from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings
from collapsarr.migrations import upgrade_to_head


def _manual_dir(settings: Settings) -> Path:
    return backups_root(settings) / BACKUP_MANUAL


def _populate_db(settings: Settings) -> None:
    """Bring the SQLite file to head so there's a real schema to snapshot."""
    upgrade_to_head(settings)
    engine = create_engine_from_settings(settings)
    engine.dispose()


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_create_backup_writes_zip_with_single_openable_sqlite_file(
    settings: Settings, tmp_path: Path
) -> None:
    """A backup is a zip under ``backups/manual`` holding one valid SQLite DB."""
    _populate_db(settings)

    info = create_backup(settings)

    archive_path = _manual_dir(settings) / info.name
    assert archive_path.exists()
    assert info.type == BACKUP_MANUAL
    assert info.size > 0
    assert info.id == f"{BACKUP_MANUAL}/{info.name}"

    # The zip holds exactly one member, and it is an openable SQLite database.
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [ARCHIVE_MEMBER_NAME]
        extracted = tmp_path / "extracted.db"
        extracted.write_bytes(archive.read(ARCHIVE_MEMBER_NAME))

    connection = sqlite3.connect(str(extracted))
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()
    # The snapshot carries the real migrated schema (sentinel settings table).
    assert "global_settings" in tables


def test_backup_filename_encodes_version_and_utc_timestamp(settings: Settings) -> None:
    _populate_db(settings)

    info = create_backup(settings)

    assert info.name.startswith(f"collapsarr_backup_v{__version__}_")
    assert info.name.endswith(".zip")


def test_create_backup_makes_all_type_dirs_via_ensure(settings: Settings) -> None:
    ensure_backup_dirs(settings)
    for backup_type in BACKUP_TYPES:
        assert (backups_root(settings) / backup_type).is_dir()


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def test_list_backups_empty_when_none_written(settings: Settings) -> None:
    assert list_backups(settings) == []


def test_list_backups_returns_written_backups_newest_first(settings: Settings) -> None:
    import os

    _populate_db(settings)

    # One real backup, plus a fabricated older archive with a distinct name
    # (the filename's second-granularity means two real backups in the same
    # second would share a name -- ordering is by mtime here).
    newest = create_backup(settings)
    older = _manual_dir(settings) / "collapsarr_backup_v0.0.1_2020.01.01_00.00.00.zip"
    older.write_bytes(b"not a real archive, only its listing matters")

    os.utime(_manual_dir(settings) / newest.name, (2_000_000_100, 2_000_000_100))
    os.utime(older, (2_000_000_000, 2_000_000_000))

    listed = list_backups(settings)
    assert [info.name for info in listed] == [newest.name, older.name]
    assert all(info.type == BACKUP_MANUAL for info in listed)


# --------------------------------------------------------------------------- #
# Atomic / temp-then-rename failure contract
# --------------------------------------------------------------------------- #
def test_mid_write_failure_leaves_no_listable_backup_and_no_partial_file(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure during the write leaves no listable backup and no stray file."""
    _populate_db(settings)

    def _boom(src: object, dst: object) -> None:  # noqa: ANN401 - test stub
        raise RuntimeError("simulated mid-write failure (COL-63)")

    # Fail at the final atomic publish, after the snapshot + zip already exist
    # as temp files -- the strongest test of the cleanup/rename contract.
    monkeypatch.setattr(backup_service.os, "replace", _boom)

    with pytest.raises(RuntimeError, match="simulated mid-write failure"):
        create_backup(settings)

    # No listable backup...
    assert list_backups(settings) == []
    # ...and no leftover files at all (temp files cleaned up in `finally`).
    manual_dir = _manual_dir(settings)
    assert list(manual_dir.iterdir()) == []
    assert list(manual_dir.glob(BACKUP_FILENAME_GLOB)) == []


def test_snapshot_is_consistent_against_live_db(settings: Settings) -> None:
    """VACUUM INTO snapshots the committed state of the live DB."""
    _populate_db(settings)
    db_file = resolve_sqlite_path(settings)
    assert db_file is not None

    # Write an extra row directly, committed, then back up: it must be captured.
    connection = sqlite3.connect(str(db_file))
    try:
        connection.execute("CREATE TABLE col63_probe (id INTEGER PRIMARY KEY, v TEXT)")
        connection.execute("INSERT INTO col63_probe (v) VALUES ('present')")
        connection.commit()
    finally:
        connection.close()

    info = create_backup(settings)
    with zipfile.ZipFile(_manual_dir(settings) / info.name) as archive:
        data = archive.read(ARCHIVE_MEMBER_NAME)
    extracted = Path(settings.data_dir) / "probe.db"
    extracted.write_bytes(data)
    connection = sqlite3.connect(str(extracted))
    try:
        value = connection.execute("SELECT v FROM col63_probe").fetchone()[0]
    finally:
        connection.close()
    assert value == "present"


# --------------------------------------------------------------------------- #
# Non-file / :memory: database no-op
# --------------------------------------------------------------------------- #
def test_is_backup_supported_false_for_non_sqlite_url(settings: Settings) -> None:
    non_file = settings.model_copy(
        update={"database_url": "postgresql://user:pass@localhost/collapsarr"}
    )
    assert is_backup_supported(non_file) is False


def test_is_backup_supported_false_for_memory(settings: Settings) -> None:
    memory_by_path = settings.model_copy(update={"database_path": ":memory:"})
    assert is_backup_supported(memory_by_path) is False
    memory_by_url = settings.model_copy(update={"database_url": "sqlite:///:memory:"})
    assert is_backup_supported(memory_by_url) is False


def test_create_backup_raises_for_non_file_database(settings: Settings) -> None:
    non_file = settings.model_copy(
        update={"database_url": "postgresql://user:pass@localhost/collapsarr"}
    )
    with pytest.raises(BackupUnavailableError):
        create_backup(non_file)
    # ...and nothing was written to disk.
    assert not backups_root(non_file).exists()


def test_create_backup_rejects_unknown_type(settings: Settings) -> None:
    _populate_db(settings)
    with pytest.raises(ValueError, match="Unknown backup type"):
        create_backup(settings, "bogus")


# --------------------------------------------------------------------------- #
# resolve_backup_path (COL-64)
# --------------------------------------------------------------------------- #
def test_resolve_backup_path_finds_a_real_archive(settings: Settings) -> None:
    _populate_db(settings)
    info = create_backup(settings)

    resolved = resolve_backup_path(settings, info.id)

    assert resolved == _manual_dir(settings) / info.name
    assert resolved.is_file()


def test_resolve_backup_path_none_for_unknown_type(settings: Settings) -> None:
    _populate_db(settings)
    info = create_backup(settings)
    assert resolve_backup_path(settings, f"bogus/{info.name}") is None


def test_resolve_backup_path_none_for_nonexistent_filename(settings: Settings) -> None:
    ensure_backup_dirs(settings)
    assert resolve_backup_path(settings, f"{BACKUP_MANUAL}/does_not_exist.zip") is None


def test_resolve_backup_path_none_when_filename_does_not_match_glob(settings: Settings) -> None:
    """A real file that just doesn't look like a backup archive is rejected too."""
    manual_dir = backup_type_dir(settings, BACKUP_MANUAL)
    manual_dir.mkdir(parents=True, exist_ok=True)
    stray = manual_dir / "not_a_backup.txt"
    stray.write_text("hello")
    assert resolve_backup_path(settings, f"{BACKUP_MANUAL}/not_a_backup.txt") is None


@pytest.mark.parametrize(
    "backup_id",
    [
        "manual",  # missing filename segment
        "manual/",  # empty filename
        "manual/.",
        "manual/..",
        "manual/../../etc/passwd",
        "manual/sub/collapsarr_backup_v1_2026.01.01_00.00.00.zip",  # embedded slash
        "manual/collapsarr_backup_v1_2026.01.01_00.00.00.zip/../../secret",
        "manual\\collapsarr_backup_v1_2026.01.01_00.00.00.zip",  # no split at all
    ],
)
def test_resolve_backup_path_rejects_malformed_or_traversal_ids(
    settings: Settings, backup_id: str
) -> None:
    ensure_backup_dirs(settings)
    assert resolve_backup_path(settings, backup_id) is None


def test_resolve_backup_path_does_not_cross_backup_type_boundaries(settings: Settings) -> None:
    """A filename that exists under one type is not found by asking for another."""
    _populate_db(settings)
    info = create_backup(settings, BACKUP_MANUAL)
    ensure_backup_dirs(settings)
    assert resolve_backup_path(settings, f"{BACKUP_SCHEDULED}/{info.name}") is None


# --------------------------------------------------------------------------- #
# delete_backup (COL-65)
# --------------------------------------------------------------------------- #
def _fabricate_archive(settings: Settings, name: str) -> Path:
    """Drop a well-named (glob-matching) file under ``manual`` so it's listable."""
    manual_dir = backup_type_dir(settings, BACKUP_MANUAL)
    manual_dir.mkdir(parents=True, exist_ok=True)
    archive = manual_dir / name
    archive.write_bytes(b"stand-in archive, only its presence matters")
    return archive


def test_floor_is_one_so_the_last_backup_is_protected() -> None:
    """Guard the assumed floor: this slice keeps at least one recovery point."""
    assert MINIMUM_BACKUP_KEEP == 1


def test_delete_backup_removes_the_file_when_above_the_floor(settings: Settings) -> None:
    _populate_db(settings)
    info = create_backup(settings)
    # A second archive keeps the total above the floor, so the delete proceeds.
    _fabricate_archive(settings, "collapsarr_backup_v0.0.1_2020.01.01_00.00.00.zip")
    assert len(list_backups(settings)) == 2

    delete_backup(settings, info.id)

    assert not (_manual_dir(settings) / info.name).exists()
    assert [b.name for b in list_backups(settings)] == [
        "collapsarr_backup_v0.0.1_2020.01.01_00.00.00.zip"
    ]


def test_delete_backup_refuses_below_floor_and_leaves_file_intact(settings: Settings) -> None:
    _populate_db(settings)
    info = create_backup(settings)
    assert len(list_backups(settings)) == 1

    with pytest.raises(BackupRetentionFloorError, match="at least"):
        delete_backup(settings, info.id)

    # Refused before touching disk: the sole backup is still present + listable.
    assert (_manual_dir(settings) / info.name).exists()
    assert [b.name for b in list_backups(settings)] == [info.name]


def test_delete_backup_unknown_id_raises_not_found(settings: Settings) -> None:
    ensure_backup_dirs(settings)
    with pytest.raises(BackupNotFoundError):
        delete_backup(settings, f"{BACKUP_MANUAL}/does_not_exist.zip")


@pytest.mark.parametrize(
    "backup_id",
    [
        "bogus/collapsarr_backup_v1_2026.01.01_00.00.00.zip",  # unknown type
        "manual/../../etc/passwd",  # traversal
        "manual/not_a_backup.txt",  # non-matching filename
    ],
)
def test_delete_backup_rejects_malformed_or_traversal_ids(
    settings: Settings, backup_id: str
) -> None:
    ensure_backup_dirs(settings)
    with pytest.raises(BackupNotFoundError):
        delete_backup(settings, backup_id)
