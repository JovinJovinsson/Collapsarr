"""Service-level tests for the backup snapshot engine (COL-63).

Mirrors the shape of ``tests/test_migration_backup.py``: exercises
:func:`collapsarr.backup.service.create_backup` and friends directly against
the isolated ``settings`` fixture (a throwaway SQLite file under ``tmp_path``),
covering the happy path, the atomic/temp-then-rename failure contract, listing,
and the non-file-database no-op.
"""

from __future__ import annotations

import os
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from collapsarr import __version__
from collapsarr.backup.service import (
    ARCHIVE_MEMBER_NAME,
    BACKUP_FILENAME_GLOB,
    BACKUP_MANUAL,
    BACKUP_SCHEDULED,
    BACKUP_TYPES,
    BACKUP_UPDATE,
    MINIMUM_BACKUP_KEEP,
    UPDATE_BACKUP_MIN_KEEP,
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
    prune_backups,
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
    monkeypatch.setattr("collapsarr.backup.service.os.replace", _boom)

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


# --------------------------------------------------------------------------- #
# Age-based retention pruning (COL-68)
# --------------------------------------------------------------------------- #
#: Fixed "now" so age math is deterministic without touching the wall clock.
_NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
_RETENTION_DAYS = 28


def _archive_name(index: int) -> str:
    """A distinct, glob-matching archive filename (names must differ per file)."""
    return f"collapsarr_backup_v0.0.1_2020.01.{index:02d}_00.00.00.zip"


def _fabricate_typed_archive(
    settings: Settings, backup_type: str, name: str, *, days_ago: float
) -> Path:
    """Drop a glob-matching archive under ``<type>/`` with an aged mtime.

    ``list_backups`` derives ``created_at`` from the file mtime, so setting the
    mtime relative to :data:`_NOW` is how these tests place a backup inside or
    outside the retention window.
    """
    type_dir = backup_type_dir(settings, backup_type)
    type_dir.mkdir(parents=True, exist_ok=True)
    archive = type_dir / name
    archive.write_bytes(b"stand-in archive, only its listing/mtime matter")
    epoch = (_NOW - timedelta(days=days_ago)).timestamp()
    os.utime(archive, (epoch, epoch))
    return archive


def _names_of(settings: Settings, backup_type: str) -> set[str]:
    return {
        info.name for info in list_backups(settings) if info.type == backup_type
    }


def test_update_guaranteed_minimum_constant_exceeds_global_floor() -> None:
    """Guard the assumed knobs: update keeps strictly more than the global floor."""
    assert MINIMUM_BACKUP_KEEP == 1
    assert UPDATE_BACKUP_MIN_KEEP == 2
    assert UPDATE_BACKUP_MIN_KEEP > MINIMUM_BACKUP_KEEP


def test_prune_deletes_backups_older_than_window(settings: Settings) -> None:
    """Backups past the retention window are deleted; in-window ones are kept."""
    # Newest is 10 days old (inside the 28-day window); three others are past it.
    _fabricate_typed_archive(settings, BACKUP_MANUAL, _archive_name(1), days_ago=40)
    _fabricate_typed_archive(settings, BACKUP_MANUAL, _archive_name(2), days_ago=35)
    _fabricate_typed_archive(settings, BACKUP_MANUAL, _archive_name(3), days_ago=30)
    keep = _fabricate_typed_archive(
        settings, BACKUP_MANUAL, _archive_name(4), days_ago=10
    )

    deleted = prune_backups(
        settings, BACKUP_MANUAL, retention_days=_RETENTION_DAYS, now=_NOW
    )

    assert {info.name for info in deleted} == {
        _archive_name(1),
        _archive_name(2),
        _archive_name(3),
    }
    assert _names_of(settings, BACKUP_MANUAL) == {keep.name}


def test_prune_keeps_in_window_backups_untouched(settings: Settings) -> None:
    """Every backup inside the window survives, regardless of count."""
    for i in range(1, 6):
        _fabricate_typed_archive(
            settings, BACKUP_MANUAL, _archive_name(i), days_ago=i
        )

    deleted = prune_backups(
        settings, BACKUP_MANUAL, retention_days=_RETENTION_DAYS, now=_NOW
    )

    assert deleted == []
    assert len(_names_of(settings, BACKUP_MANUAL)) == 5


def test_prune_floor_survives_when_every_backup_is_past_the_window(
    settings: Settings,
) -> None:
    """The minimum-keep floor is honoured even when all backups are expired."""
    _fabricate_typed_archive(settings, BACKUP_MANUAL, _archive_name(1), days_ago=60)
    _fabricate_typed_archive(settings, BACKUP_MANUAL, _archive_name(2), days_ago=50)
    newest = _fabricate_typed_archive(
        settings, BACKUP_MANUAL, _archive_name(3), days_ago=40
    )

    prune_backups(settings, BACKUP_MANUAL, retention_days=_RETENTION_DAYS, now=_NOW)

    # Exactly the floor survives, and it is the newest of the expired backups.
    survivors = _names_of(settings, BACKUP_MANUAL)
    assert survivors == {newest.name}
    assert len(survivors) == MINIMUM_BACKUP_KEEP


def test_prune_noop_at_exactly_floor_count(settings: Settings) -> None:
    """A single expired backup (== floor) is never pruned (empty candidate slice)."""
    only = _fabricate_typed_archive(
        settings, BACKUP_MANUAL, _archive_name(1), days_ago=99
    )

    deleted = prune_backups(
        settings, BACKUP_MANUAL, retention_days=_RETENTION_DAYS, now=_NOW
    )

    assert deleted == []
    assert _names_of(settings, BACKUP_MANUAL) == {only.name}


def test_prune_at_floor_plus_one_deletes_exactly_one(settings: Settings) -> None:
    """floor+1 expired backups: only the single over-floor one is pruned.

    Regression guard for COL-60's negative-slice bug -- a ``[:len - keep]``
    slice here would have deleted the newest (kept) backup instead of the
    oldest over-floor one.
    """
    older = _fabricate_typed_archive(
        settings, BACKUP_MANUAL, _archive_name(1), days_ago=50
    )
    newest = _fabricate_typed_archive(
        settings, BACKUP_MANUAL, _archive_name(2), days_ago=40
    )

    deleted = prune_backups(
        settings, BACKUP_MANUAL, retention_days=_RETENTION_DAYS, now=_NOW
    )

    assert {info.name for info in deleted} == {older.name}
    assert _names_of(settings, BACKUP_MANUAL) == {newest.name}


def test_prune_update_type_keeps_guaranteed_minimum_regardless_of_age(
    settings: Settings,
) -> None:
    """``update`` backups keep UPDATE_BACKUP_MIN_KEEP even when all are expired."""
    _fabricate_typed_archive(settings, BACKUP_UPDATE, _archive_name(1), days_ago=90)
    _fabricate_typed_archive(settings, BACKUP_UPDATE, _archive_name(2), days_ago=80)
    keep_a = _fabricate_typed_archive(
        settings, BACKUP_UPDATE, _archive_name(3), days_ago=70
    )
    keep_b = _fabricate_typed_archive(
        settings, BACKUP_UPDATE, _archive_name(4), days_ago=60
    )

    prune_backups(settings, BACKUP_UPDATE, retention_days=_RETENTION_DAYS, now=_NOW)

    # The two most-recent update backups survive despite being far past the window.
    survivors = _names_of(settings, BACKUP_UPDATE)
    assert survivors == {keep_a.name, keep_b.name}
    assert len(survivors) == UPDATE_BACKUP_MIN_KEEP


def test_prune_update_type_still_deletes_beyond_the_guaranteed_minimum(
    settings: Settings,
) -> None:
    """Past the guaranteed-minimum, ``update`` backups still prune by age."""
    # Two inside the window (kept by the floor), one inside, one expired.
    keep_1 = _fabricate_typed_archive(
        settings, BACKUP_UPDATE, _archive_name(1), days_ago=5
    )
    keep_2 = _fabricate_typed_archive(
        settings, BACKUP_UPDATE, _archive_name(2), days_ago=10
    )
    keep_3 = _fabricate_typed_archive(
        settings, BACKUP_UPDATE, _archive_name(3), days_ago=20
    )
    expired = _fabricate_typed_archive(
        settings, BACKUP_UPDATE, _archive_name(4), days_ago=40
    )

    deleted = prune_backups(
        settings, BACKUP_UPDATE, retention_days=_RETENTION_DAYS, now=_NOW
    )

    assert {info.name for info in deleted} == {expired.name}
    assert _names_of(settings, BACKUP_UPDATE) == {
        keep_1.name,
        keep_2.name,
        keep_3.name,
    }


def test_prune_never_removes_the_protected_backup(settings: Settings) -> None:
    """``protect`` shields a backup that would otherwise be pruned by age."""
    _fabricate_typed_archive(settings, BACKUP_MANUAL, _archive_name(1), days_ago=10)
    doomed = _fabricate_typed_archive(
        settings, BACKUP_MANUAL, _archive_name(2), days_ago=40
    )
    protected = _fabricate_typed_archive(
        settings, BACKUP_MANUAL, _archive_name(3), days_ago=50
    )

    deleted = prune_backups(
        settings,
        BACKUP_MANUAL,
        retention_days=_RETENTION_DAYS,
        now=_NOW,
        protect=protected,
    )

    # The 40-day archive is pruned; the protected 50-day one is spared.
    assert {info.name for info in deleted} == {doomed.name}
    assert protected.name in _names_of(settings, BACKUP_MANUAL)


def test_prune_operates_per_type_only(settings: Settings) -> None:
    """Pruning one type never touches archives of another type."""
    _fabricate_typed_archive(settings, BACKUP_MANUAL, _archive_name(1), days_ago=40)
    _fabricate_typed_archive(settings, BACKUP_MANUAL, _archive_name(2), days_ago=10)
    sched_old = _fabricate_typed_archive(
        settings, BACKUP_SCHEDULED, _archive_name(3), days_ago=90
    )
    sched_new = _fabricate_typed_archive(
        settings, BACKUP_SCHEDULED, _archive_name(4), days_ago=80
    )

    prune_backups(settings, BACKUP_MANUAL, retention_days=_RETENTION_DAYS, now=_NOW)

    # scheduled/ is fully intact even though both its archives are expired.
    assert _names_of(settings, BACKUP_SCHEDULED) == {sched_old.name, sched_new.name}


def test_prune_disabled_for_non_positive_retention(settings: Settings) -> None:
    """A non-positive window disables age pruning (defensive guard)."""
    _fabricate_typed_archive(settings, BACKUP_MANUAL, _archive_name(1), days_ago=99)
    _fabricate_typed_archive(settings, BACKUP_MANUAL, _archive_name(2), days_ago=88)

    assert prune_backups(settings, BACKUP_MANUAL, retention_days=0, now=_NOW) == []
    assert len(_names_of(settings, BACKUP_MANUAL)) == 2


def test_prune_rejects_unknown_type(settings: Settings) -> None:
    with pytest.raises(ValueError, match="Unknown backup type"):
        prune_backups(settings, "bogus", retention_days=_RETENTION_DAYS)


# --------------------------------------------------------------------------- #
# create_backup wires pruning into the create path (COL-68)
# --------------------------------------------------------------------------- #
def test_create_backup_prunes_stale_and_never_the_just_created_one(
    settings: Settings,
) -> None:
    """A create with ``retention_days`` prunes expired archives, keeps the new one."""
    _populate_db(settings)
    # A pre-existing expired manual archive that the prune should remove.
    stale = _fabricate_typed_archive(
        settings, BACKUP_MANUAL, _archive_name(1), days_ago=99
    )

    info = create_backup(settings, BACKUP_MANUAL, retention_days=_RETENTION_DAYS)

    survivors = _names_of(settings, BACKUP_MANUAL)
    assert stale.name not in survivors  # expired archive pruned
    assert info.name in survivors  # the just-created backup is protected


def test_create_backup_without_retention_days_does_not_prune(
    settings: Settings,
) -> None:
    """Omitting ``retention_days`` leaves existing backups untouched (compat)."""
    _populate_db(settings)
    stale = _fabricate_typed_archive(
        settings, BACKUP_MANUAL, _archive_name(1), days_ago=99
    )

    info = create_backup(settings, BACKUP_MANUAL)

    survivors = _names_of(settings, BACKUP_MANUAL)
    assert stale.name in survivors  # no prune requested -> old archive stays
    assert info.name in survivors
