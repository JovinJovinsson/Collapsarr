"""Smoke tests for the application skeleton, plus the FFmpeg startup health
check and its /health surfacing (COL-38)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr import __version__
from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory, init_db
from collapsarr.health import FfmpegCheckResult
from collapsarr.main import create_app
from collapsarr.notify.service import update_notifier_config


def test_health_returns_ok(client: TestClient) -> None:
    """GET /health returns 200 with a JSON status payload.

    The real ``ffmpeg`` binary is expected to be present in the dev/CI
    environment (the downmix pipeline's own tests already rely on this, e.g.
    ``tests/test_downmix_remux.py``), so the default ``client`` fixture's
    startup check should find it and report "ok" with no warnings.
    """
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["warnings"] == []


def test_app_wires_database_state(client: TestClient) -> None:
    """The lifespan sets up the engine and session factory on app.state."""
    app = client.app
    assert isinstance(app, FastAPI)
    assert app.state.engine is not None
    assert app.state.session_factory is not None


# ---------------------------------------------------------------------------
# AC: app checks for FFmpeg presence on startup, present + missing paths.
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_ffmpeg(settings: Settings) -> Iterator[TestClient]:
    """A client whose startup check reports FFmpeg present (no real lookup)."""
    check = FfmpegCheckResult(
        available=True, ffmpeg_path="ffmpeg", detail="FFmpeg found at '/usr/bin/ffmpeg'."
    )
    app = create_app(settings=settings, ffmpeg_checker=lambda: check)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client_without_ffmpeg(settings: Settings) -> Iterator[TestClient]:
    """A client whose startup check reports FFmpeg missing (no real lookup)."""
    check = FfmpegCheckResult(
        available=False,
        ffmpeg_path="ffmpeg",
        detail="FFmpeg executable 'ffmpeg' was not found on PATH.",
    )
    app = create_app(settings=settings, ffmpeg_checker=lambda: check)
    with TestClient(app) as test_client:
        yield test_client


def test_startup_check_runs_and_stores_result_on_app_state(client_with_ffmpeg: TestClient) -> None:
    """The FFmpeg check runs once at startup and its result is stashed on
    app.state (read by the /health route), not recomputed per-request."""
    app = client_with_ffmpeg.app
    assert isinstance(app, FastAPI)
    assert app.state.ffmpeg_check.available is True


def test_health_is_ok_with_no_warnings_when_ffmpeg_is_present(
    client_with_ffmpeg: TestClient,
) -> None:
    response = client_with_ffmpeg.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["warnings"] == []


def test_health_is_degraded_with_a_warning_when_ffmpeg_is_missing(
    client_without_ffmpeg: TestClient,
) -> None:
    response = client_without_ffmpeg.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert len(body["warnings"]) == 1
    warning = body["warnings"][0]
    assert warning["code"] == "ffmpeg_missing"
    assert "not found on PATH" in warning["message"]


def test_health_route_is_unauthenticated_even_when_degraded(
    client_without_ffmpeg: TestClient,
) -> None:
    """The /health probe stays open (no API key required) whether ok or
    degraded -- COL-26's auth middleware only guards /api routes."""
    response = client_without_ffmpeg.get("/health")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# AC: a missing FFmpeg at startup also triggers the notifier dispatch, if
# notifiers are configured/enabled -- exercised end-to-end through the real
# app lifespan (collapsarr.main.create_app), not just the health.py bridge
# unit (see tests/test_health_check.py for that).
# ---------------------------------------------------------------------------


def test_app_startup_dispatches_a_notification_when_ffmpeg_is_missing_and_a_notifier_is_enabled(
    settings: Settings,
) -> None:
    # Pre-seed an enabled webhook notifier before the app (re-)opens this same
    # SQLite file in its own lifespan-owned engine/session.
    engine = create_engine_from_settings(settings)
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as setup_session:
        update_notifier_config(
            setup_session, webhook_url="https://example.com/hook", webhook_enabled=True
        )
    engine.dispose()

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    missing_check = FfmpegCheckResult(
        available=False,
        ffmpeg_path="ffmpeg",
        detail="FFmpeg executable 'ffmpeg' was not found on PATH.",
    )
    app = create_app(
        settings=settings,
        ffmpeg_checker=lambda: missing_check,
        notify_transport=httpx.MockTransport(handler),
    )

    with TestClient(app):  # entering the context runs the lifespan/startup
        pass

    assert len(seen) == 1
    payload = json.loads(seen[0].content)
    assert payload["event_type"] == "health_check_failed"


def test_app_startup_makes_no_network_call_when_ffmpeg_is_missing_but_no_notifier_enabled(
    settings: Settings,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    missing_check = FfmpegCheckResult(available=False, ffmpeg_path="ffmpeg", detail="missing")
    app = create_app(
        settings=settings,
        ffmpeg_checker=lambda: missing_check,
        notify_transport=httpx.MockTransport(handler),
    )

    with TestClient(app):
        pass

    assert seen == []


def test_app_startup_makes_no_network_call_when_ffmpeg_is_present_even_with_a_notifier_enabled(
    settings: Settings,
) -> None:
    engine = create_engine_from_settings(settings)
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as setup_session:
        update_notifier_config(
            setup_session, webhook_url="https://example.com/hook", webhook_enabled=True
        )
    engine.dispose()

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    present_check = FfmpegCheckResult(available=True, ffmpeg_path="ffmpeg", detail="found")
    app = create_app(
        settings=settings,
        ffmpeg_checker=lambda: present_check,
        notify_transport=httpx.MockTransport(handler),
    )

    with TestClient(app):
        pass

    assert seen == []
