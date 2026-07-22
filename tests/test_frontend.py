"""Tests for the bundled single-page frontend serving (COL-40).

The real assets are only present in a built wheel (force-included from
``frontend/dist``); these tests exercise the mount behaviour against a
temporary stand-in directory so they pass in a plain source checkout.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr.frontend import mount_frontend


def _write_built_frontend(directory: Path) -> None:
    """Create a minimal built-frontend layout (index.html + an asset)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text("<!doctype html><title>Collapsarr</title>")
    assets = directory / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('collapsarr')")


def test_mount_frontend_returns_false_when_assets_absent(tmp_path: Path) -> None:
    app = FastAPI()
    assert mount_frontend(app, tmp_path / "missing") is False


def test_mount_frontend_serves_index_and_assets(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    _write_built_frontend(static_dir)
    app = FastAPI()
    assert mount_frontend(app, static_dir) is True
    client = TestClient(app)

    root = client.get("/")
    assert root.status_code == 200
    assert "Collapsarr" in root.text

    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert "collapsarr" in asset.text


def test_spa_fallback_serves_index_for_unknown_route(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    _write_built_frontend(static_dir)
    app = FastAPI()
    mount_frontend(app, static_dir)
    client = TestClient(app)

    resp = client.get("/instances/42")
    assert resp.status_code == 200
    assert "Collapsarr" in resp.text


def test_api_routes_take_precedence_over_spa_mount(tmp_path: Path) -> None:
    """The catch-all SPA mount must not shadow API/health routes."""
    static_dir = tmp_path / "static"
    _write_built_frontend(static_dir)
    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    mount_frontend(app, static_dir)
    client = TestClient(app)

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
