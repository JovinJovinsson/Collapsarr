"""Contract tests for the backup REST endpoints (COL-63).

Covers ``GET /api/system/backup`` and ``POST /api/system/backup`` through the
shared ``client`` :class:`~fastapi.testclient.TestClient` fixture: the API-gate
behaviour (401 without a key), the create -> list round-trip against the real
service, and the "unavailable for this database configuration" surface for a
non-file-based database.
"""

from __future__ import annotations

import os
import threading
import zipfile
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

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


# --------------------------------------------------------------------------- #
# Download (COL-64)
# --------------------------------------------------------------------------- #
def test_download_requires_authentication(client: TestClient) -> None:
    # No API key / session: the /api gate rejects the download route too.
    response = client.get(f"/api/system/backup/{BACKUP_MANUAL}/whatever.zip/download")
    assert response.status_code == 401


def test_download_streams_the_correct_archive(client: TestClient, settings: Settings) -> None:
    headers = _auth_headers(client)
    created = client.post("/api/system/backup", headers=headers).json()

    response = client.get(f"/api/system/backup/{created['id']}/download", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert created["name"] in disposition

    archive_path = backups_root(settings) / BACKUP_MANUAL / created["name"]
    assert response.content == archive_path.read_bytes()
    assert int(response.headers["content-length"]) == archive_path.stat().st_size

    # The streamed bytes really are the same valid zip written to disk.
    with zipfile.ZipFile(Path(archive_path)) as archive:
        assert archive.namelist() == ["collapsarr.db"]


def test_download_unknown_type_returns_404(client: TestClient) -> None:
    headers = _auth_headers(client)
    response = client.get("/api/system/backup/bogus/whatever.zip/download", headers=headers)
    assert response.status_code == 404


def test_download_nonexistent_filename_returns_404(client: TestClient) -> None:
    headers = _auth_headers(client)
    response = client.get(
        f"/api/system/backup/{BACKUP_MANUAL}/collapsarr_backup_v9.9.9_2020.01.01_00.00.00.zip/download",
        headers=headers,
    )
    assert response.status_code == 404


def test_download_path_traversal_id_returns_404(client: TestClient) -> None:
    headers = _auth_headers(client)
    response = client.get(
        "/api/system/backup/manual/../../etc/passwd/download",
        headers=headers,
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Large download through the full middleware stack (COL-197)
# --------------------------------------------------------------------------- #
_LARGE_DOWNLOAD_PAYLOAD_SIZE = 12 * 1024 * 1024
"""~190 of FileResponse's 64 KiB send chunks -- large enough to actually
exercise chunked streaming rather than complete in a single ``send()``."""

_LARGE_DOWNLOAD_JOIN_TIMEOUT = 30.0
"""Bound on how long the background-thread download is allowed to run.

If the middleware stack really does stall (rather than merely mis-count
bytes), the request never returns and the thread never finishes -- joining
with a timeout turns that into a clear, bounded test failure instead of
hanging the suite forever."""


def test_large_backup_download_completes_through_the_full_middleware_stack(
    client: TestClient, settings: Settings
) -> None:
    """Reproduction test for the reported "stalls at 100%" download bug (COL-197).

    Backup downloads reportedly hang in Safari/Chrome after appearing to reach
    100%. The leading hypothesis is ``enforce_auth_middleware``: it is wired
    onto the app via ``app.middleware("http")``, which Starlette turns into a
    :class:`~starlette.middleware.base.BaseHTTPMiddleware` that re-buffers the
    wrapped response body through its own ``anyio`` task-group/memory-stream
    machinery instead of handing the ASGI ``send`` callable straight through
    to :class:`~fastapi.responses.FileResponse` -- and that this mishandles a
    large streamed body.

    Unlike ``test_download_streams_the_correct_archive`` above (a handful of
    real bytes), this drives the request through the *whole* registered
    middleware stack -- ``enforce_auth_middleware``, ``SessionMiddleware``,
    and ``UrlBaseMiddleware`` -- against a multi-megabyte synthetic payload,
    not just the route handler in isolation. The download runs on a
    background thread with a bounded ``join`` (see
    ``_LARGE_DOWNLOAD_JOIN_TIMEOUT``) so a genuine stall fails the test
    explicitly instead of hanging the suite.

    If this test fails (times out, or the bytes/content-length don't match),
    that confirms the middleware hypothesis and this becomes the regression
    test for the fix (COL-199). If it passes, the hypothesis needs revisiting
    before COL-199 proceeds.
    """
    headers = _auth_headers(client)
    created = client.post("/api/system/backup", headers=headers).json()

    # Overwrite the tiny real archive with a large synthetic payload. The
    # download route only cares about what bytes are on disk at the resolved
    # path -- it doesn't need to still be a valid zip -- so this is a cheap
    # way to get a multi-MB body without minutes of real database growth.
    archive_path = backups_root(settings) / BACKUP_MANUAL / created["name"]
    payload = os.urandom(_LARGE_DOWNLOAD_PAYLOAD_SIZE)
    archive_path.write_bytes(payload)

    outcome: dict[str, object] = {}

    def _download() -> None:
        try:
            outcome["response"] = client.get(
                f"/api/system/backup/{created['id']}/download", headers=headers
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread below
            outcome["error"] = exc

    thread = threading.Thread(target=_download, daemon=True)
    thread.start()
    thread.join(timeout=_LARGE_DOWNLOAD_JOIN_TIMEOUT)
    assert not thread.is_alive(), (
        f"backup download did not complete within {_LARGE_DOWNLOAD_JOIN_TIMEOUT:.0f}s "
        "through the full middleware stack -- reproduces the reported "
        "stalled-download bug (COL-197)"
    )

    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]

    response = outcome["response"]
    assert isinstance(response, httpx.Response)
    assert response.status_code == 200
    assert int(response.headers["content-length"]) == _LARGE_DOWNLOAD_PAYLOAD_SIZE
    assert len(response.content) == _LARGE_DOWNLOAD_PAYLOAD_SIZE
    assert response.content == payload


# --------------------------------------------------------------------------- #
# Delete (COL-65)
# --------------------------------------------------------------------------- #
def test_delete_requires_authentication(client: TestClient) -> None:
    # No API key / session: the /api gate rejects the delete route too.
    response = client.delete(f"/api/system/backup/{BACKUP_MANUAL}/whatever.zip")
    assert response.status_code == 401


def test_delete_removes_the_backup_when_above_the_floor(
    client: TestClient, settings: Settings
) -> None:
    headers = _auth_headers(client)
    created = client.post("/api/system/backup", headers=headers).json()

    # A second on-disk archive keeps the total above the minimum-keep floor so
    # the delete is permitted (two real backups would collide on filename).
    manual_dir = backups_root(settings) / BACKUP_MANUAL
    (manual_dir / "collapsarr_backup_v0.0.1_2020.01.01_00.00.00.zip").write_bytes(b"stand-in")

    response = client.delete(f"/api/system/backup/{created['id']}", headers=headers)
    assert response.status_code == 204

    # The file is gone from disk and no longer listed.
    assert not (manual_dir / created["name"]).exists()
    listed = client.get("/api/system/backup", headers=headers).json()
    assert [b["name"] for b in listed["backups"]] == [
        "collapsarr_backup_v0.0.1_2020.01.01_00.00.00.zip"
    ]


def test_delete_below_floor_is_refused_with_409_and_keeps_the_file(
    client: TestClient, settings: Settings
) -> None:
    headers = _auth_headers(client)
    created = client.post("/api/system/backup", headers=headers).json()

    # Only one backup exists: deleting it would breach the minimum-keep floor.
    response = client.delete(f"/api/system/backup/{created['id']}", headers=headers)
    assert response.status_code == 409
    assert "kept" in response.json()["detail"].lower()

    # Refused without touching disk: still on disk and still listed.
    assert (backups_root(settings) / BACKUP_MANUAL / created["name"]).exists()
    listed = client.get("/api/system/backup", headers=headers).json()
    assert [b["name"] for b in listed["backups"]] == [created["name"]]


def test_delete_unknown_type_returns_404(client: TestClient) -> None:
    headers = _auth_headers(client)
    response = client.delete("/api/system/backup/bogus/whatever.zip", headers=headers)
    assert response.status_code == 404


def test_delete_nonexistent_filename_returns_404(client: TestClient) -> None:
    headers = _auth_headers(client)
    response = client.delete(
        f"/api/system/backup/{BACKUP_MANUAL}/collapsarr_backup_v9.9.9_2020.01.01_00.00.00.zip",
        headers=headers,
    )
    assert response.status_code == 404


def test_delete_path_traversal_id_returns_404(client: TestClient) -> None:
    headers = _auth_headers(client)
    response = client.delete("/api/system/backup/manual/../../etc/passwd", headers=headers)
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# No static exposure of backups/ (COL-64)
# --------------------------------------------------------------------------- #
def test_backups_directory_has_no_static_mount(client: TestClient, settings: Settings) -> None:
    """The authenticated download route is the *only* way to fetch a backup.

    Confirmed two ways: (1) no ``Mount`` registered on the app serves the
    backups directory (or an ancestor of it) as static files, and (2) probing
    a plausible "raw" filesystem-shaped path for a real archive does not
    return the file (200 with the zip bytes) the way a static mount would.
    """
    app = client.app
    assert isinstance(app, FastAPI)
    backups_dir = backups_root(settings).resolve()

    for route in app.router.routes:
        if isinstance(route, Mount):
            mount_dir = getattr(route.app, "directory", None)
            if mount_dir is None:
                continue
            mount_dir = Path(mount_dir).resolve()
            assert mount_dir != backups_dir
            assert backups_dir not in mount_dir.parents
            assert mount_dir not in backups_dir.parents

    headers = _auth_headers(client)
    created = client.post("/api/system/backup", headers=headers).json()

    # No credential exists yet in this fresh test app, so unauthenticated
    # browser-route probes hit the first-run redirect rather than any static
    # file -- either way, never the zip.
    probe = client.get(f"/backups/{created['id']}", follow_redirects=False)
    assert probe.status_code != 200
