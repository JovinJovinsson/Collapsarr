"""Tests for the COLLAPSARR_URL_BASE strip-prefix ASGI middleware (COL-116).

Covers the prefixed, unprefixed, and no-url-base ``TestClient``-level cases
per the ticket's acceptance criteria; ``tests/test_config.py`` covers the
``Settings`` validation/normalization. Prior art for the mount-style
``TestClient`` setup: ``tests/test_frontend.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import httpx
import pytest
from fastapi.testclient import TestClient

from collapsarr.config import Settings
from collapsarr.health import DiskUsage
from collapsarr.main import create_app


class _FakeDiskUsage(NamedTuple):
    total: int
    used: int
    free: int


def _ample_free_space(_path: str) -> DiskUsage:
    """Deterministic disk-usage stand-in (see tests/conftest.py's fixture)."""
    return _FakeDiskUsage(total=1000, used=100, free=900)


def _offline_update_check_transport() -> httpx.MockTransport:
    """Deterministic, offline stand-in for the Update Check scheduler's fetch."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="offline in tests")

    return httpx.MockTransport(handler)


def _make_client(tmp_path: Path, *, url_base: str = "") -> TestClient:
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "collapsarr.db"),
        data_dir=str(tmp_path),
        url_base=url_base,
    )
    app = create_app(
        settings=settings,
        disk_usage=_ample_free_space,
        update_check_transport=_offline_update_check_transport(),
    )
    return TestClient(app)


def test_no_url_base_configured_behaves_exactly_as_today(tmp_path: Path) -> None:
    """With no url_base set, /health resolves unchanged (default behaviour)."""
    with _make_client(tmp_path) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] in ("ok", "degraded")


def test_prefixed_health_resolves_with_url_base_configured(tmp_path: Path) -> None:
    """<url_base>/health resolves exactly as /health does today (COL-116)."""
    with _make_client(tmp_path, url_base="/collapsarr") as client:
        response = client.get("/collapsarr/health")
        assert response.status_code == 200
        assert response.json()["status"] in ("ok", "degraded")


def test_prefixed_api_route_resolves_with_url_base_configured(tmp_path: Path) -> None:
    """<url_base>/api/... resolves exactly as /api/... does today (COL-116)."""
    with _make_client(tmp_path, url_base="/collapsarr") as client:
        response = client.get("/collapsarr/api/auth/status")
        assert response.status_code == 200


def test_unprefixed_health_still_resolves_with_url_base_configured(tmp_path: Path) -> None:
    """Direct/unprefixed traffic (e.g. a Docker healthcheck) keeps working
    even once a url_base is configured -- deliberate per ADR-0004."""
    with _make_client(tmp_path, url_base="/collapsarr") as client:
        response = client.get("/health")
        assert response.status_code == 200


def test_partial_prefix_match_does_not_strip(tmp_path: Path) -> None:
    """A path that merely starts with the same characters as url_base, but
    isn't actually prefixed by it as a path segment, is not stripped."""
    with _make_client(tmp_path, url_base="/collapsarr") as client:
        response = client.get("/collapsarrbogus/health")
        assert response.status_code == 404


def test_openapi_reflects_url_base_via_root_path(tmp_path: Path) -> None:
    """/openapi.json's servers entry reflects the configured prefix, fed by
    ASGI root_path (COL-116)."""
    with _make_client(tmp_path, url_base="/collapsarr") as client:
        response = client.get("/collapsarr/openapi.json")
        assert response.status_code == 200
        assert response.json()["servers"] == [{"url": "/collapsarr"}]


def test_openapi_has_no_servers_entry_when_no_url_base(tmp_path: Path) -> None:
    """With no url_base, OpenAPI generation is unaffected (COL-116)."""
    with _make_client(tmp_path) as client:
        response = client.get("/openapi.json")
        assert response.status_code == 200
        assert response.json().get("servers") in (None, [])


@pytest.mark.parametrize("prefixed_path", ["/collapsarr", "/collapsarr/"])
def test_exact_prefix_root_strips_to_app_root(tmp_path: Path, prefixed_path: str) -> None:
    """A request to exactly the url_base (with or without trailing slash)
    strips to "/" and resolves identically to an unprefixed "/" request on
    the same app -- not a distinct, unmatched route."""
    with _make_client(tmp_path, url_base="/collapsarr") as client:
        prefixed = client.get(prefixed_path, follow_redirects=False)
        root = client.get("/", follow_redirects=False)
        assert prefixed.status_code == root.status_code
        assert prefixed.headers.get("location") == root.headers.get("location")
