"""Tests for the bundled single-page frontend serving (COL-40).

The real assets are only present in a built wheel (force-included from
``frontend/dist``); these tests exercise the mount behaviour against a
temporary stand-in directory so they pass in a plain source checkout.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr.frontend import _inject_url_base, mount_frontend

# A realistic Vite-style built document: a <head>/</head> pair and the app
# bundle loaded as a module <script>. The url_base injection lands the runtime
# global immediately before that first <script>, so it runs before the bundle
# (COL-118).
_INDEX_HTML = (
    "<!doctype html>\n"
    '<html lang="en">\n'
    "  <head>\n"
    "    <title>Collapsarr</title>\n"
    "  </head>\n"
    "  <body>\n"
    '    <div id="root"></div>\n'
    '    <script type="module" src="/assets/app.js"></script>\n'
    "  </body>\n"
    "</html>"
)


def _write_built_frontend(directory: Path) -> None:
    """Create a minimal built-frontend layout (index.html + an asset)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(_INDEX_HTML)
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


_RUNTIME_GLOBAL = "window.__COLLAPSARR_URL_BASE__"


def test_url_base_injected_into_served_index(tmp_path: Path) -> None:
    """With url_base set, index.html carries the runtime global (COL-118)."""
    static_dir = tmp_path / "static"
    _write_built_frontend(static_dir)
    app = FastAPI()
    mount_frontend(app, static_dir, url_base="/collapsarr")
    client = TestClient(app)

    body = client.get("/").text
    assert f'{_RUNTIME_GLOBAL} = "/collapsarr";' in body
    # Injected before the app bundle loads.
    assert body.index(_RUNTIME_GLOBAL) < body.index('src="/assets/app.js"')
    # Still the SPA document (nothing clobbered).
    assert "Collapsarr" in body


def test_url_base_injected_on_spa_fallback(tmp_path: Path) -> None:
    """The runtime global is present on the client-route fallback too."""
    static_dir = tmp_path / "static"
    _write_built_frontend(static_dir)
    app = FastAPI()
    mount_frontend(app, static_dir, url_base="/collapsarr")
    client = TestClient(app)

    resp = client.get("/libraries/42")
    assert resp.status_code == 200
    assert f'{_RUNTIME_GLOBAL} = "/collapsarr";' in resp.text


def test_inject_url_base_escapes_hostile_value() -> None:
    """A url_base with quotes/angle brackets cannot break out of the script.

    ``_normalize_url_base`` only enforces the leading/trailing slash, so an
    operator-configured value may contain ``"`` or ``</script>``. Such a value
    must be encoded as a valid JS string literal that stays inside the injected
    ``<script>`` tag (COL-118 review must-fix).
    """
    hostile = '/foo"bar</script><script>alert(1)</script>'
    out = _inject_url_base(_INDEX_HTML, hostile)

    # None of the hostile ``</script>``/``<script>`` payload survives literally:
    # every ``<`` was escaped, so nothing could terminate the injected tag.
    assert "</script><script>alert" not in out
    assert '"bar</script>' not in out
    assert "<script>alert(1)" not in out

    # The emitted literal parses back to exactly the original value: extract the
    # assignment ``= <literal>;`` and round-trip it (``<`` -> ``<``).
    marker = f"{_RUNTIME_GLOBAL} = "
    start = out.index(marker) + len(marker)
    end = out.index(";</script>", start)
    assert json.loads(out[start:end]) == hostile


def test_index_unchanged_without_url_base(tmp_path: Path) -> None:
    """No url_base -> index.html is served byte-for-byte as on disk (COL-118)."""
    static_dir = tmp_path / "static"
    _write_built_frontend(static_dir)
    app = FastAPI()
    mount_frontend(app, static_dir)
    client = TestClient(app)

    on_disk = (static_dir / "index.html").read_text()
    assert client.get("/").text == on_disk
    assert _RUNTIME_GLOBAL not in client.get("/").text
