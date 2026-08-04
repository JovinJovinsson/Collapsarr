"""Contract tests for the restore-request endpoint (COL-71).

Covers ``POST /api/system/backup/restore/{id}``: the auth gate, an unknown id
returning ``404``, every branch of the validate-before-stage gate returning
``422`` with the running instance left completely untouched (no marker, no
staged file, shutdown never triggered), and the full success path -- stage via
the endpoint, then boot a second app instance (mirroring
``tests/test_restore_engine.py``'s pattern) to prove the marker really is
consumed and the restored data is served, forward-migrated to head.

``trigger_shutdown`` is monkeypatched to a no-op in every test that reaches
the success path -- the real implementation kills the process, which must
never happen inside the test runner.
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr.backup.service import (
    ARCHIVE_MEMBER_NAME,
    BACKUP_MANUAL,
    BACKUP_UPDATE,
    backup_type_dir,
    list_backups,
)
from collapsarr.config import Settings
from collapsarr.main import create_app
from collapsarr.migrations import BASELINE_REVISION, build_alembic_config
from collapsarr.restore.marker import read_restore_marker, restore_marker_path
from collapsarr.restore.request import restore_staging_path
from collapsarr.settings.service import get_global_settings


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _insert_instance(db_path: Path, name: str) -> None:
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


def _create_manual_backup_of(client: TestClient, headers: dict[str, str]) -> str:
    """Create a manual backup of the app's current live database and return its id."""
    response = client.post("/api/system/backup", headers=headers)
    assert response.status_code == 202
    return str(response.json()["id"])


def _write_bogus_archive(settings: Settings, filename: str, content: bytes) -> str:
    """Write a bogus file directly under backups/manual/ matching the naming
    convention (so :func:`resolve_backup_path` finds it), and return its id.
    """
    manual_dir = backup_type_dir(settings, BACKUP_MANUAL)
    manual_dir.mkdir(parents=True, exist_ok=True)
    (manual_dir / filename).write_bytes(content)
    return f"{BACKUP_MANUAL}/{filename}"


def _zip_with_member(member_name: str, content: bytes) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, content)
    return buffer.getvalue()


def _write_old_revision_backup(settings: Settings, tmp_path: Path, *, name: str) -> str:
    """Build a real backup archive whose DB is stamped at the baseline revision
    (older than head) carrying a sentinel ``name`` row, and return its id.

    Mirrors ``tests/test_restore_engine.py``'s ``_build_staged_db`` helper, but
    zips the result into ``backups/manual/`` the way a real archive would look,
    so it can be restored *through the endpoint* rather than via a hand-written
    marker.
    """
    old_db = tmp_path / "old_revision.db"
    old_settings = Settings(database_path=str(old_db), data_dir=str(tmp_path))
    command.upgrade(build_alembic_config(old_settings), BASELINE_REVISION)
    _insert_instance(old_db, name)

    zip_bytes = _zip_with_member(ARCHIVE_MEMBER_NAME, old_db.read_bytes())
    filename = "collapsarr_backup_v0.1.0_2020.01.01_00.00.00.zip"
    return _write_bogus_archive(settings, filename, zip_bytes)


def _write_newer_revision_backup(settings: Settings, tmp_path: Path, *, name: str) -> str:
    """Build a real backup archive whose DB is stamped at a revision no packaged
    migration defines -- the signature of a backup taken by a *newer* Collapsarr.

    Built from a head-schema DB whose ``alembic_version`` is then overwritten to
    a bogus revision, so the file still passes the SQLite + sentinel-table gates
    and is only rejected by the COL-72 version guard.
    """
    newer_db = tmp_path / "newer_revision.db"
    newer_settings = Settings(database_path=str(newer_db), data_dir=str(tmp_path))
    command.upgrade(build_alembic_config(newer_settings), "head")
    _insert_instance(newer_db, name)
    connection = sqlite3.connect(str(newer_db))
    try:
        connection.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
        connection.commit()
    finally:
        connection.close()

    zip_bytes = _zip_with_member(ARCHIVE_MEMBER_NAME, newer_db.read_bytes())
    filename = "collapsarr_backup_v9.9.9_2099.01.01_00.00.00.zip"
    return _write_bogus_archive(settings, filename, zip_bytes)


# --------------------------------------------------------------------------- #
# Auth gate + unknown id
# --------------------------------------------------------------------------- #
def test_restore_requires_authentication(client: TestClient) -> None:
    response = client.post(f"/api/system/backup/restore/{BACKUP_MANUAL}/whatever.zip")
    assert response.status_code == 401


def test_restore_unknown_id_returns_404(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.restore.routes.trigger_shutdown", lambda: None)
    headers = _auth_headers(client)

    response = client.post(
        f"/api/system/backup/restore/{BACKUP_MANUAL}/"
        "collapsarr_backup_v9.9.9_2020.01.01_00.00.00.zip",
        headers=headers,
    )

    assert response.status_code == 404
    assert not restore_marker_path(settings).exists()


def test_restore_path_traversal_id_returns_404(client: TestClient) -> None:
    headers = _auth_headers(client)
    # httpx normalizes ".." segments before the request is even sent, same as
    # the equivalent download/delete traversal tests in test_backup_routes.py.
    # Three ".." are needed here (one more than those routes) to walk fully
    # past this route's extra "restore/" path segment, so the collapsed path
    # matches no registered route at all rather than accidentally landing on
    # a *different* verb's route (which would 405, not 404).
    response = client.post(
        "/api/system/backup/restore/manual/../../../etc/passwd", headers=headers
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Validate-before-stage gate: every rejection leaves the instance untouched
# --------------------------------------------------------------------------- #
def test_restore_rejects_a_non_zip_archive(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutdown_calls: list[None] = []
    monkeypatch.setattr(
        "collapsarr.restore.routes.trigger_shutdown", lambda: shutdown_calls.append(None)
    )
    headers = _auth_headers(client)
    backup_id = _write_bogus_archive(
        settings, "collapsarr_backup_v0.1.0_2026.01.01_00.00.00.zip", b"not a zip at all"
    )

    response = client.post(f"/api/system/backup/restore/{backup_id}", headers=headers)

    assert response.status_code == 422
    assert "not a valid zip" in response.json()["detail"].lower()
    assert not restore_marker_path(settings).exists()
    assert not restore_staging_path(settings).exists()
    assert shutdown_calls == []


def test_restore_rejects_a_zip_missing_the_db_entry(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.restore.routes.trigger_shutdown", lambda: None)
    headers = _auth_headers(client)
    zip_bytes = _zip_with_member("wrong_name.db", b"irrelevant")
    backup_id = _write_bogus_archive(
        settings, "collapsarr_backup_v0.1.0_2026.01.02_00.00.00.zip", zip_bytes
    )

    response = client.post(f"/api/system/backup/restore/{backup_id}", headers=headers)

    assert response.status_code == 422
    assert "does not contain" in response.json()["detail"].lower()
    assert not restore_marker_path(settings).exists()


def test_restore_rejects_a_non_sqlite_db_member(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.restore.routes.trigger_shutdown", lambda: None)
    headers = _auth_headers(client)
    zip_bytes = _zip_with_member(ARCHIVE_MEMBER_NAME, b"this is definitely not sqlite")
    backup_id = _write_bogus_archive(
        settings, "collapsarr_backup_v0.1.0_2026.01.03_00.00.00.zip", zip_bytes
    )

    response = client.post(f"/api/system/backup/restore/{backup_id}", headers=headers)

    assert response.status_code == 422
    assert "not a valid sqlite" in response.json()["detail"].lower()
    assert not restore_marker_path(settings).exists()
    assert not restore_staging_path(settings).exists()


def test_restore_rejects_a_sqlite_db_missing_the_sentinel_table(
    client: TestClient, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real, openable SQLite file with no ``global_settings`` table fails the gate."""
    monkeypatch.setattr("collapsarr.restore.routes.trigger_shutdown", lambda: None)
    headers = _auth_headers(client)

    empty_db = tmp_path / "empty.db"
    connection = sqlite3.connect(str(empty_db))
    connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    zip_bytes = _zip_with_member(ARCHIVE_MEMBER_NAME, empty_db.read_bytes())
    backup_id = _write_bogus_archive(
        settings, "collapsarr_backup_v0.1.0_2026.01.04_00.00.00.zip", zip_bytes
    )

    response = client.post(f"/api/system/backup/restore/{backup_id}", headers=headers)

    assert response.status_code == 422
    assert "global_settings" in response.json()["detail"]
    assert not restore_marker_path(settings).exists()
    assert not restore_staging_path(settings).exists()


def test_restore_rejects_a_newer_version_backup(
    client: TestClient, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Version guard (COL-72): a backup stamped at a revision this build doesn't
    know is rejected at stage time with a clear "newer version" 422 -- no marker,
    no staged file, and shutdown is never triggered.
    """
    shutdown_calls: list[None] = []
    monkeypatch.setattr(
        "collapsarr.restore.routes.trigger_shutdown", lambda: shutdown_calls.append(None)
    )
    headers = _auth_headers(client)

    _insert_instance(Path(settings.database_path), "CURRENT")
    backup_id = _write_newer_revision_backup(settings, tmp_path, name="FUTURE")

    response = client.post(f"/api/system/backup/restore/{backup_id}", headers=headers)

    assert response.status_code == 422
    assert "newer version" in response.json()["detail"].lower()
    # Nothing was armed and the running instance is completely untouched.
    assert not restore_marker_path(settings).exists()
    assert not restore_staging_path(settings).exists()
    assert shutdown_calls == []
    assert _instance_names(Path(settings.database_path)) == ["CURRENT"]


# --------------------------------------------------------------------------- #
# Success: stage via the endpoint, then a second boot swaps + serves it
# --------------------------------------------------------------------------- #
def test_restore_stages_and_arms_the_marker_without_touching_the_live_db(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutdown_calls: list[None] = []
    monkeypatch.setattr(
        "collapsarr.restore.routes.trigger_shutdown", lambda: shutdown_calls.append(None)
    )
    headers = _auth_headers(client)

    _insert_instance(Path(settings.database_path), "CURRENT")
    backup_id = _create_manual_backup_of(client, headers)

    response = client.post(f"/api/system/backup/restore/{backup_id}", headers=headers)

    assert response.status_code == 202
    body = response.json()
    assert body == {"status": "restoring", "backup_id": backup_id}

    # The marker is armed and points at a real, staged database...
    marker = read_restore_marker(settings)
    assert marker is not None
    assert marker.staged_path == restore_staging_path(settings).resolve()
    assert marker.staged_path.is_file()

    # ...but the *running* instance's own database was never touched by this
    # request -- it is still serving CURRENT, and shutdown was triggered
    # exactly once (the process itself didn't actually die, since the test
    # monkeypatched it away).
    assert _instance_names(Path(settings.database_path)) == ["CURRENT"]
    assert shutdown_calls == [None]


def test_stage_then_boot_swaps_in_the_restored_backup_and_forward_migrates(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: restore an older backup via the endpoint, then a fresh boot
    consumes the marker, swaps it in, and forward-migrates it to head.
    """
    monkeypatch.setattr("collapsarr.restore.routes.trigger_shutdown", lambda: None)
    headers = _auth_headers(client)

    # The live app currently serves "OLD" -- take a manual backup of it, this
    # is the recovery point we'll restore back to.
    _insert_instance(Path(settings.database_path), "OLD")
    old_backup_id = _create_manual_backup_of(client, headers)

    # Simulate further (unwanted) changes to the live database.
    _insert_instance(Path(settings.database_path), "UNWANTED")
    assert _instance_names(Path(settings.database_path)) == ["OLD", "UNWANTED"]

    # Restore the OLD backup via the endpoint.
    response = client.post(f"/api/system/backup/restore/{old_backup_id}", headers=headers)
    assert response.status_code == 202

    # A fresh app instance boots (mirrors what the supervisor's restart does):
    # apply_pending_restore consumes the marker before upgrade_to_head runs.
    second_app = create_app(settings=settings)
    with TestClient(second_app) as second_client:
        assert second_client.get("/health").status_code == 200
        assert _instance_names(Path(settings.database_path)) == ["OLD"]

    # Marker is consumed; staged file gone; safety backup of the pre-restore
    # (OLD+UNWANTED) state is listable.
    assert not restore_marker_path(settings).exists()
    assert not restore_staging_path(settings).exists()
    restore_type_backups = [b for b in list_backups(settings) if b.type == "restore"]
    assert len(restore_type_backups) == 1


def test_restoring_an_older_revision_backup_is_forward_migrated_on_next_boot(
    client: TestClient, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup archive whose DB predates head is migrated forward after the swap.

    Builds a real archive stamped at :data:`BASELINE_REVISION` (older than
    head) and restores it *through the endpoint* -- not by hand-writing a
    marker -- so this specifically exercises COL-71's handoff into COL-70's
    existing forward-migrate-on-boot behaviour.
    """
    monkeypatch.setattr("collapsarr.restore.routes.trigger_shutdown", lambda: None)
    headers = _auth_headers(client)

    _insert_instance(Path(settings.database_path), "CURRENT")
    old_backup_id = _write_old_revision_backup(settings, tmp_path, name="OLD")

    response = client.post(f"/api/system/backup/restore/{old_backup_id}", headers=headers)
    assert response.status_code == 202

    second_app = create_app(settings=settings)
    with TestClient(second_app) as second_client:
        assert second_client.get("/health").status_code == 200

    live_db = Path(settings.database_path)
    head_revision = ScriptDirectory.from_config(build_alembic_config(settings)).get_current_head()
    assert _instance_names(live_db) == ["OLD"]

    connection = sqlite3.connect(str(live_db))
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        connection.close()
    assert row is not None and row[0] == head_revision

    # A pre-migration `update` backup was also taken of the swapped-in (older)
    # database, alongside the pre-swap `restore` safety backup.
    by_type = {b.type for b in list_backups(settings)}
    assert BACKUP_UPDATE in by_type
