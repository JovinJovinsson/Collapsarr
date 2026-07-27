"""Contract tests for the backup REST endpoints (COL-63).

Covers ``GET /api/system/backup`` and ``POST /api/system/backup`` through the
shared ``client`` :class:`~fastapi.testclient.TestClient` fixture: the API-gate
behaviour (401 without a key), the create -> list round-trip against the real
service, and the "unavailable for this database configuration" surface for a
non-file-based database.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr.backup.service import BACKUP_MANUAL, backups_root
from collapsarr.config import Settings
from collapsarr.main import create_app
from collapsarr.settings.service import get_global_settings


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #
def test_endpoints_require_authentication(client: TestClient) -> None:
    # No API key / session: the /api gate rejects both verbs.
    assert client.get("/api/system/backup").status_code == 401
    assert client.post("/api/system/backup").status_code == 401


# --------------------------------------------------------------------------- #
# Create -> list round-trip
# --------------------------------------------------------------------------- #
def test_list_is_empty_before_any_backup(client: TestClient) -> None:
    response = client.get("/api/system/backup", headers=_auth_headers(client))
    assert response.status_code == 200
    body = response.json()
    assert body == {"supported": True, "backups": []}


def test_post_creates_backup_returns_202_and_appears_in_list(
    client: TestClient, settings: Settings
) -> None:
    headers = _auth_headers(client)

    created = client.post("/api/system/backup", headers=headers)
    assert created.status_code == 202
    summary = created.json()
    assert summary["type"] == BACKUP_MANUAL
    assert summary["size"] > 0
    assert summary["name"].startswith("collapsarr_backup_v")
    assert summary["id"] == f"{BACKUP_MANUAL}/{summary['name']}"
    assert "created_at" in summary

    # The archive really landed under <data_dir>/backups/manual/ as a valid zip.
    archive_path = backups_root(settings) / BACKUP_MANUAL / summary["name"]
    assert archive_path.exists()
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["collapsarr.db"]

    listed = client.get("/api/system/backup", headers=headers).json()
    assert listed["supported"] is True
    assert [b["name"] for b in listed["backups"]] == [summary["name"]]


# --------------------------------------------------------------------------- #
# Non-file database -> unavailable
# --------------------------------------------------------------------------- #
def test_non_file_database_reports_unsupported_and_409_on_create(tmp_path: Path) -> None:
    """A non-file-based DB surfaces the unavailable state, not backup controls.

    Uses a file-based SQLite DB for the app's own storage (so it can boot) but
    overrides only the backup service's view via app.state settings by pointing
    the whole install at an in-memory-equivalent config is awkward; instead we
    build the app normally, then flip app.state.settings to a :memory: config
    that the endpoints read for the supported/create decision.
    """
    db_path = tmp_path / "collapsarr.db"
    boot_settings = Settings(database_path=str(db_path), data_dir=str(tmp_path))
    app = create_app(settings=boot_settings)
    with TestClient(app) as client:
        headers = _auth_headers(client)
        # Swap the settings the endpoints consult to a non-file (memory) config.
        app.state.settings = boot_settings.model_copy(
            update={"database_url": "sqlite:///:memory:"}
        )

        listed = client.get("/api/system/backup", headers=headers).json()
        assert listed == {"supported": False, "backups": []}

        created = client.post("/api/system/backup", headers=headers)
        assert created.status_code == 409
        assert "unavailable" in created.json()["detail"].lower()
