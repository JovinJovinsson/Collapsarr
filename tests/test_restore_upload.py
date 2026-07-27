"""Contract tests for the upload-restore endpoint (COL-73).

Covers ``POST /api/system/backup/restore/upload``: the auth gate, the full
success path (upload a real archive, then boot a second app instance to prove
the marker is consumed and the restored data served), and every hardened-input
rejection this untrusted surface must enforce -- zip-slip, oversize raw upload,
oversize uncompressed (decompression bomb), a newer-than-supported revision,
and the plain malformed cases. Every rejection must leave the running instance
completely untouched: no marker, no staged file, shutdown never triggered.

``trigger_shutdown`` is monkeypatched to a no-op (or a call tracker) in every
test -- the real implementation kills the process, which must never happen
inside the test runner.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path

import pytest
from alembic import command
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr.backup.service import (
    ARCHIVE_MEMBER_NAME,
    list_backups,
    resolve_backup_path,
)
from collapsarr.config import Settings
from collapsarr.main import create_app
from collapsarr.migrations import BASELINE_REVISION, build_alembic_config
from collapsarr.restore.marker import read_restore_marker, restore_marker_path
from collapsarr.restore.request import restore_staging_path
from collapsarr.settings.service import get_global_settings

UPLOAD_URL = "/api/system/backup/restore/upload"


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _no_op_shutdown(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    calls: list[None] = []
    monkeypatch.setattr(
        "collapsarr.restore.routes.trigger_shutdown", lambda: calls.append(None)
    )
    return calls


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


def _zip_with_member(member_name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, content)
    return buffer.getvalue()


def _valid_archive_bytes(client: TestClient, settings: Settings, headers: dict[str, str]) -> bytes:
    """Take a real manual backup of the live DB and return the archive's bytes.

    The bytes of a genuine, gate-passing Collapsarr backup archive -- used as
    the happy-path upload body.
    """
    response = client.post("/api/system/backup", headers=headers)
    assert response.status_code == 202
    backup_id = str(response.json()["id"])
    path = resolve_backup_path(settings, backup_id)
    assert path is not None
    return path.read_bytes()


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #
def test_upload_restore_requires_authentication(client: TestClient) -> None:
    response = client.post(UPLOAD_URL, content=b"anything")
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Success: upload a real archive, then a second boot swaps + serves it
# --------------------------------------------------------------------------- #
def test_upload_restore_stages_and_arms_the_marker_without_touching_the_live_db(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutdown_calls = _no_op_shutdown(monkeypatch)
    headers = _auth_headers(client)

    _insert_instance(Path(settings.database_path), "CURRENT")
    archive = _valid_archive_bytes(client, settings, headers)

    response = client.post(UPLOAD_URL, content=archive, headers=headers)

    assert response.status_code == 202
    assert response.json() == {"status": "restoring"}

    marker = read_restore_marker(settings)
    assert marker is not None
    assert marker.staged_path == restore_staging_path(settings).resolve()
    assert marker.staged_path.is_file()

    # The running instance's own database was never touched, shutdown fired once.
    assert _instance_names(Path(settings.database_path)) == ["CURRENT"]
    assert shutdown_calls == [None]
    # The streaming temp file was cleaned up.
    assert not (Path(settings.data_dir) / ".restore_upload.zip.part").exists()


def test_upload_then_boot_swaps_in_the_restored_backup(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: upload an archive of an earlier state, then a fresh boot
    consumes the marker and swaps it in, discarding later live changes.
    """
    monkeypatch.setattr("collapsarr.restore.routes.trigger_shutdown", lambda: None)
    headers = _auth_headers(client)

    # Snapshot the live DB while it holds only OLD -- this archive is our
    # recovery point.
    _insert_instance(Path(settings.database_path), "OLD")
    archive = _valid_archive_bytes(client, settings, headers)

    # Simulate later unwanted changes to the live database.
    _insert_instance(Path(settings.database_path), "UNWANTED")
    assert _instance_names(Path(settings.database_path)) == ["OLD", "UNWANTED"]

    response = client.post(UPLOAD_URL, content=archive, headers=headers)
    assert response.status_code == 202

    # A fresh app instance boots (mirrors the supervisor's restart): the marker
    # is consumed before upgrade_to_head runs.
    second_app = create_app(settings=settings)
    with TestClient(second_app) as second_client:
        assert second_client.get("/health").status_code == 200
        assert _instance_names(Path(settings.database_path)) == ["OLD"]

    assert not restore_marker_path(settings).exists()
    assert not restore_staging_path(settings).exists()
    # A pre-swap `restore` safety backup of the OLD+UNWANTED state exists.
    assert [b for b in list_backups(settings) if b.type == "restore"]


# --------------------------------------------------------------------------- #
# Hardened-input rejections: each leaves the running instance untouched
# --------------------------------------------------------------------------- #
def test_upload_restore_rejects_zip_slip_entry(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive carrying a path-traversal entry is rejected outright, even
    when it also contains a valid DB member.
    """
    shutdown_calls = _no_op_shutdown(monkeypatch)
    headers = _auth_headers(client)
    _insert_instance(Path(settings.database_path), "CURRENT")
    valid_db = _valid_archive_bytes(client, settings, headers)
    inner_db = zipfile.ZipFile(io.BytesIO(valid_db)).read(ARCHIVE_MEMBER_NAME)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(ARCHIVE_MEMBER_NAME, inner_db)
        archive.writestr("../../etc/passwd", b"pwned")
    malicious = buffer.getvalue()

    response = client.post(UPLOAD_URL, content=malicious, headers=headers)

    assert response.status_code == 422
    assert "zip-slip" in response.json()["detail"].lower()
    assert not restore_marker_path(settings).exists()
    assert not restore_staging_path(settings).exists()
    assert shutdown_calls == []
    assert _instance_names(Path(settings.database_path)) == ["CURRENT"]


def test_upload_restore_rejects_an_absolute_path_entry(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_op_shutdown(monkeypatch)
    headers = _auth_headers(client)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("/etc/cron.d/evil", b"pwned")
    response = client.post(UPLOAD_URL, content=buffer.getvalue(), headers=headers)

    assert response.status_code == 422
    assert "zip-slip" in response.json()["detail"].lower()
    assert not restore_marker_path(settings).exists()


def test_upload_restore_rejects_an_oversized_archive(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw upload larger than the archive cap is refused with a 413 before
    extraction, and never spooled unbounded to disk.
    """
    shutdown_calls = _no_op_shutdown(monkeypatch)
    monkeypatch.setattr("collapsarr.restore.upload.MAX_UPLOAD_ARCHIVE_BYTES", 16)
    headers = _auth_headers(client)

    response = client.post(UPLOAD_URL, content=b"x" * 4096, headers=headers)

    assert response.status_code == 413
    assert "maximum allowed size" in response.json()["detail"].lower()
    assert not restore_marker_path(settings).exists()
    assert not restore_staging_path(settings).exists()
    assert shutdown_calls == []
    assert not (Path(settings.data_dir) / ".restore_upload.zip.part").exists()


def test_upload_restore_rejects_a_decompression_bomb(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB entry whose uncompressed size exceeds the cap is rejected (422)."""
    shutdown_calls = _no_op_shutdown(monkeypatch)
    monkeypatch.setattr("collapsarr.restore.upload.MAX_UNCOMPRESSED_BYTES", 16)
    headers = _auth_headers(client)
    _insert_instance(Path(settings.database_path), "CURRENT")
    archive = _valid_archive_bytes(client, settings, headers)

    response = client.post(UPLOAD_URL, content=archive, headers=headers)

    assert response.status_code == 422
    assert "uncompressed size" in response.json()["detail"].lower()
    assert not restore_marker_path(settings).exists()
    assert not restore_staging_path(settings).exists()
    assert shutdown_calls == []


def test_upload_restore_rejects_a_newer_revision_archive(
    client: TestClient, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Version guard (COL-72) applies on the upload path too: a DB stamped at a
    revision this build doesn't know is rejected with a clear 422.
    """
    shutdown_calls = _no_op_shutdown(monkeypatch)
    headers = _auth_headers(client)
    _insert_instance(Path(settings.database_path), "CURRENT")

    newer_db = tmp_path / "newer.db"
    newer_settings = Settings(database_path=str(newer_db), data_dir=str(tmp_path))
    command.upgrade(build_alembic_config(newer_settings), "head")
    _insert_instance(newer_db, "FUTURE")
    connection = sqlite3.connect(str(newer_db))
    try:
        connection.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
        connection.commit()
    finally:
        connection.close()
    archive = _zip_with_member(ARCHIVE_MEMBER_NAME, newer_db.read_bytes())

    response = client.post(UPLOAD_URL, content=archive, headers=headers)

    assert response.status_code == 422
    assert "newer version" in response.json()["detail"].lower()
    assert not restore_marker_path(settings).exists()
    assert not restore_staging_path(settings).exists()
    assert shutdown_calls == []
    assert _instance_names(Path(settings.database_path)) == ["CURRENT"]


def test_upload_restore_forward_migrates_an_older_revision_on_next_boot(
    client: TestClient, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uploaded archive whose DB predates head passes the gate and is
    migrated forward after the boot swap.
    """
    monkeypatch.setattr("collapsarr.restore.routes.trigger_shutdown", lambda: None)
    headers = _auth_headers(client)
    _insert_instance(Path(settings.database_path), "CURRENT")

    old_db = tmp_path / "old.db"
    old_settings = Settings(database_path=str(old_db), data_dir=str(tmp_path))
    command.upgrade(build_alembic_config(old_settings), BASELINE_REVISION)
    _insert_instance(old_db, "OLD")
    archive = _zip_with_member(ARCHIVE_MEMBER_NAME, old_db.read_bytes())

    response = client.post(UPLOAD_URL, content=archive, headers=headers)
    assert response.status_code == 202

    second_app = create_app(settings=settings)
    with TestClient(second_app) as second_client:
        assert second_client.get("/health").status_code == 200
    assert _instance_names(Path(settings.database_path)) == ["OLD"]


def test_upload_restore_rejects_a_non_zip_body(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_op_shutdown(monkeypatch)
    headers = _auth_headers(client)

    response = client.post(UPLOAD_URL, content=b"this is not a zip file at all", headers=headers)

    assert response.status_code == 422
    assert "not a valid zip" in response.json()["detail"].lower()
    assert not restore_marker_path(settings).exists()


def test_upload_restore_rejects_a_zip_missing_the_db_entry(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_op_shutdown(monkeypatch)
    headers = _auth_headers(client)
    archive = _zip_with_member("wrong_name.db", b"irrelevant")

    response = client.post(UPLOAD_URL, content=archive, headers=headers)

    assert response.status_code == 422
    assert "does not contain" in response.json()["detail"].lower()
    assert not restore_marker_path(settings).exists()


def test_upload_restore_rejects_an_empty_body(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_op_shutdown(monkeypatch)
    headers = _auth_headers(client)

    response = client.post(UPLOAD_URL, content=b"", headers=headers)

    assert response.status_code == 422
    assert not restore_marker_path(settings).exists()
