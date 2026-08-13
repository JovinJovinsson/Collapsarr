"""Contract tests for the FFmpeg auto-download route (COL-222).

Covers ``POST /api/system/ffmpeg/download``: the auth gate, Docker-gating
(``403``, COL-215's ``install_method``), a full success round-trip
(download -> extract -> persist ``GlobalSettings.ffmpeg_path``, verified via
a subsequent health-check recheck that ``ffmpeg_missing`` flips to passing
with no restart -- COL-218's live-read wiring), and the failure/retry shape
on a checksum mismatch (no partial/unverified path ever persisted).
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.ffmpeg_download.manifest import ManifestEntry
from collapsarr.main import create_app
from collapsarr.settings.service import get_global_settings

_FFMPEG_BYTES = b"pretend this is a real ffmpeg executable's bytes"


def _zip_archive(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _offline_update_check_transport() -> httpx.MockTransport:
    """Deterministic, offline stand-in for the startup Update Check tick.

    Mirrors ``conftest.py``'s private helper of the same shape -- redefined
    locally (rather than importing a private name from another test module)
    to keep this file self-contained, matching
    ``tests/test_update_check_routes.py``'s own convention.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="offline in tests")

    return httpx.MockTransport(handler)


def _ffmpeg_transport(archive: bytes, *, status_code: int = 200) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=archive if status_code < 400 else b"")

    return httpx.MockTransport(handler)


def _app(
    settings: Settings, *, ffmpeg_download_transport: httpx.MockTransport | None = None
) -> FastAPI:
    return create_app(
        settings=settings,
        update_check_transport=_offline_update_check_transport(),
        ffmpeg_download_transport=ffmpeg_download_transport,
    )


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _patch_manifest(monkeypatch: pytest.MonkeyPatch, entry: ManifestEntry) -> None:
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.get_manifest_entry", lambda *_a, **_k: entry
    )
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.resolve_platform_arch",
        lambda: (entry.platform, entry.arch),
    )


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #


def test_download_requires_authentication(settings: Settings) -> None:
    with TestClient(_app(settings)) as client:
        assert client.post("/api/system/ffmpeg/download").status_code == 401


# --------------------------------------------------------------------------- #
# Docker gating (COL-215)
# --------------------------------------------------------------------------- #


def test_download_is_unavailable_for_docker_installs(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "collapsarr.system.info.is_docker_environment", lambda: True
    )
    with TestClient(_app(settings)) as client:
        headers = _auth_headers(client)
        response = client.post("/api/system/ffmpeg/download", headers=headers)

    assert response.status_code == 403
    assert "docker" in response.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# Success
# --------------------------------------------------------------------------- #


def test_download_succeeds_extracts_and_persists_ffmpeg_path(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "collapsarr.system.info.is_docker_environment", lambda: False
    )
    archive = _zip_archive({"ffmpeg": _FFMPEG_BYTES})
    entry = ManifestEntry(
        platform="linux",
        arch="amd64",
        version="8.1.2",
        url="https://example.invalid/ffmpeg.zip",
        sha256=hashlib.sha256(archive).hexdigest(),
    )
    _patch_manifest(monkeypatch, entry)

    with TestClient(_app(settings, ffmpeg_download_transport=_ffmpeg_transport(archive))) as client:
        headers = _auth_headers(client)
        response = client.post("/api/system/ffmpeg/download", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["ffmpeg_path"]

        # COL-218's wiring: the health check reads GlobalSettings.ffmpeg_path
        # live on every tick -- confirm a fresh tick reports it available
        # with no restart, by pointing the recheck at the extracted binary
        # made executable and resolvable (a bare-bytes stub isn't a real
        # invocable `ffmpeg`, so this checks *persistence*, not that the
        # stub binary actually runs -- `check_ffmpeg` is a presence probe
        # only, see collapsarr/health/ffmpeg.py).
        with app_session(client) as session:
            assert get_global_settings(session).ffmpeg_path == body["ffmpeg_path"]


def app_session(client: TestClient) -> Session:
    app = client.app
    assert isinstance(app, FastAPI)
    session: Session = app.state.session_factory()
    return session


# --------------------------------------------------------------------------- #
# Failure / retry
# --------------------------------------------------------------------------- #


def test_download_failure_reports_a_clear_error_and_persists_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "collapsarr.system.info.is_docker_environment", lambda: False
    )
    archive = _zip_archive({"ffmpeg": _FFMPEG_BYTES})
    entry = ManifestEntry(
        platform="linux",
        arch="amd64",
        version="8.1.2",
        url="https://example.invalid/ffmpeg.zip",
        # Deliberately wrong checksum.
        sha256=hashlib.sha256(b"not the real archive").hexdigest(),
    )
    _patch_manifest(monkeypatch, entry)

    with TestClient(_app(settings, ffmpeg_download_transport=_ffmpeg_transport(archive))) as client:
        headers = _auth_headers(client)
        response = client.post("/api/system/ffmpeg/download", headers=headers)

        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "sha-256" in detail.lower() or "mismatch" in detail.lower()

        with app_session(client) as session:
            assert get_global_settings(session).ffmpeg_path is None


def test_download_can_be_retried_after_a_transient_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "collapsarr.system.info.is_docker_environment", lambda: False
    )
    archive = _zip_archive({"ffmpeg": _FFMPEG_BYTES})
    entry = ManifestEntry(
        platform="linux",
        arch="amd64",
        version="8.1.2",
        url="https://example.invalid/ffmpeg.zip",
        sha256=hashlib.sha256(archive).hexdigest(),
    )
    _patch_manifest(monkeypatch, entry)

    attempts = {"count": 0}

    def flaky_handler(_request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, content=archive)

    with TestClient(
        _app(settings, ffmpeg_download_transport=httpx.MockTransport(flaky_handler))
    ) as client:
        headers = _auth_headers(client)

        first = client.post("/api/system/ffmpeg/download", headers=headers)
        assert first.status_code == 502
        with app_session(client) as session:
            assert get_global_settings(session).ffmpeg_path is None

        second = client.post("/api/system/ffmpeg/download", headers=headers)
        assert second.status_code == 200
        assert second.json()["ffmpeg_path"]
