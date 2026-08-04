"""Smoke tests for the application skeleton, plus the Health Check Framework's
startup tick, its /health surfacing, and its transition notifications (COL-75).

The default ``client`` fixture builds the app with the scheduler disabled, so a
single synchronous health tick runs during startup (see
``collapsarr.main.create_app``'s lifespan) -- enough to populate /health and
fire any pass->fail notification deterministically, without a background thread.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import NamedTuple

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr import __version__
from collapsarr.arr.models import InstanceType
from collapsarr.arr.service import create_instance
from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.health import DiskUsage, FfmpegCheckResult
from collapsarr.main import create_app
from collapsarr.migrations import upgrade_to_head
from collapsarr.notify.service import update_notifier_config


def _arr_ok_transport() -> httpx.MockTransport:
    """A transport reporting any Arr instance as reachable."""
    return httpx.MockTransport(lambda request: httpx.Response(200, json={"version": "4.0.0"}))


def _offline_update_check_transport() -> httpx.MockTransport:
    """A deterministic stand-in for the Update Check scheduler's GitHub fetch.

    Mirrors ``conftest.py``'s ``_offline_update_check_transport``: the app
    lifespan also runs an ``UpdateCheckScheduler.run_once()`` tick
    synchronously on startup (COL-86), which -- since COL-89 -- shares this
    module's ``notify_transport`` mock for its own edge-triggered "update
    available" notification. Left unpinned, that tick would hit the real
    ``api.github.com`` and, on a successful first-ever fetch, fire an
    unrelated notification through the very mock these tests use to assert
    ffmpeg-only notification counts. Reporting a fixed failure keeps that tick
    a guaranteed no-op (no tag, no transition, no notification), scoping these
    tests back to ffmpeg alone.
    """
    return httpx.MockTransport(lambda _request: httpx.Response(503, text="offline in tests"))


class _FakeUsage(NamedTuple):
    total: int
    used: int
    free: int


def _ample_free_space() -> Callable[[str], DiskUsage]:
    """A disk-usage probe reporting 90% free -- well clear of the disk-space
    check's (COL-79) default 5%/2% thresholds.

    The disk-space check is now registered by default, so a completely fresh
    app -- as every isolated ``settings`` fixture starts out -- would
    otherwise reflect *this host's real, possibly near-full* disk, adding its
    own warning/notification on top of whatever these ffmpeg-focused tests
    are asserting (mirrors ``_seed_arr_instance``'s rationale for COL-77/78
    above). Every ``create_app(...)`` call below passes this so their
    assertions stay scoped to ffmpeg alone.
    """
    usage = _FakeUsage(total=1000, used=100, free=900)
    return lambda _path: usage


def _seed_arr_instance(settings: Settings) -> None:
    """Pre-configure one Arr instance against ``settings``' database.

    The no-Arr-instances-configured check (COL-77) is now registered by
    default, so a completely fresh database -- as every isolated ``settings``
    fixture starts out -- would otherwise add its own warning/notification on
    top of whatever these ffmpeg-focused tests are asserting. Seeding one
    instance up front keeps those assertions scoped to ffmpeg alone, matching
    their original intent from COL-75.

    Every ``create_app(...)`` call following this seed must also pass
    ``arr_transport=_arr_ok_transport()`` -- the per-instance Arr-unreachable
    check (COL-78) re-probes this seeded instance live on every startup tick
    (it never reads the ``status`` column this creation call stamps), so
    without an "always reachable" transport it would otherwise make a real
    network call to this fake host and add its own unrelated failure/
    notification on top of these ffmpeg-focused assertions.
    """
    engine = create_engine_from_settings(settings)
    upgrade_to_head(settings)
    session_factory = create_session_factory(engine)
    with session_factory() as setup_session:
        create_instance(
            setup_session,
            name="Seed Sonarr",
            instance_type=InstanceType.SONARR,
            base_url="http://sonarr.local:8989",
            api_key="seed-api-key",
            transport=_arr_ok_transport(),
        )
    engine.dispose()


def test_health_returns_ok(settings: Settings) -> None:
    """GET /health returns 200 with a JSON status payload.

    The real ``ffmpeg`` binary is expected to be present in the dev/CI
    environment (the downmix pipeline's own tests already rely on this, e.g.
    ``tests/test_downmix_remux.py``), so a freshly built app's startup check
    should find it and report "ok" with no warnings. One Arr instance is
    seeded first so the no-Arr-instances check (COL-77) doesn't add its own
    warning here -- this test builds its own app (rather than using the
    shared ``client`` fixture) so the seed lands before the lifespan's
    startup tick runs.
    """
    _seed_arr_instance(settings)
    app = create_app(
        settings=settings, arr_transport=_arr_ok_transport(), disk_usage=_ample_free_space()
    )
    with TestClient(app) as test_client:
        response = test_client.get("/health")

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
    """A client whose startup check reports FFmpeg present (no real lookup).

    Seeds one Arr instance first so the no-Arr-instances check (COL-77) stays
    passing here too -- these fixtures are scoped to ffmpeg alone.
    """
    _seed_arr_instance(settings)
    check = FfmpegCheckResult(
        available=True, ffmpeg_path="ffmpeg", detail="FFmpeg found at '/usr/bin/ffmpeg'."
    )
    app = create_app(
        settings=settings,
        ffmpeg_checker=lambda: check,
        arr_transport=_arr_ok_transport(),
        disk_usage=_ample_free_space(),
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client_without_ffmpeg(settings: Settings) -> Iterator[TestClient]:
    """A client whose startup check reports FFmpeg missing (no real lookup).

    Seeds one Arr instance first so the no-Arr-instances check (COL-77) stays
    passing here, keeping the "degraded" assertions below scoped to the single
    ffmpeg warning they're testing.
    """
    _seed_arr_instance(settings)
    check = FfmpegCheckResult(
        available=False,
        ffmpeg_path="ffmpeg",
        detail="FFmpeg executable 'ffmpeg' was not found on PATH.",
    )
    app = create_app(
        settings=settings,
        ffmpeg_checker=lambda: check,
        arr_transport=_arr_ok_transport(),
        disk_usage=_ample_free_space(),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_startup_runs_a_health_tick_and_exposes_the_scheduler(
    client_with_ffmpeg: TestClient,
) -> None:
    """The framework runs a synchronous tick at startup (populating /health from
    persisted state) and exposes the scheduler on app.state."""
    app = client_with_ffmpeg.app
    assert isinstance(app, FastAPI)
    assert app.state.health_scheduler is not None


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


def test_health_reflects_the_check_synchronously_at_startup_with_the_scheduler_enabled(
    settings: Settings,
) -> None:
    """With the real background scheduler enabled, /health must already reflect
    the first check result the instant the app comes up -- before any background
    tick fires.

    This guards the deterministic-at-startup guarantee (the pre-framework
    startup check ran synchronously before serving). The lifespan runs the first
    tick synchronously and only then starts the daemon thread for the *recurring*
    5-minute cadence (``run_immediately=False``), so that thread will not tick
    again for a full interval. Hence a "degraded" /health the moment the context
    is entered can only have come from the synchronous startup tick -- not a race
    with the background thread. Were the first tick left to the thread (the
    regression), /health could momentarily report "ok" here.
    """
    _seed_arr_instance(settings)
    missing_check = FfmpegCheckResult(
        available=False,
        ffmpeg_path="ffmpeg",
        detail="FFmpeg executable 'ffmpeg' was not found on PATH.",
    )
    app = create_app(
        settings=settings,
        ffmpeg_checker=lambda: missing_check,
        enable_scheduler=True,
        arr_transport=_arr_ok_transport(),
        disk_usage=_ample_free_space(),
    )

    with TestClient(app) as test_client:  # entering the context runs the lifespan
        response = test_client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert len(body["warnings"]) == 1
    assert body["warnings"][0]["code"] == "ffmpeg_missing"


def test_health_route_is_unauthenticated_even_when_degraded(
    client_without_ffmpeg: TestClient,
) -> None:
    """The /health probe stays open (no API key required) whether ok or
    degraded -- COL-26's auth middleware only guards /api routes."""
    response = client_without_ffmpeg.get("/health")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# AC: a missing FFmpeg at startup also triggers a transition notification, if
# notifiers are configured/enabled -- exercised end-to-end through the real app
# lifespan (collapsarr.main.create_app)'s synchronous startup tick, not just the
# reconcile unit (see tests/test_health_check.py for that).
# ---------------------------------------------------------------------------


def test_app_startup_dispatches_a_notification_when_ffmpeg_is_missing_and_a_notifier_is_enabled(
    settings: Settings,
) -> None:
    # Pre-seed one Arr instance (so the COL-77 check stays passing and only
    # ffmpeg's transition is under test here) and an enabled webhook notifier,
    # before the app (re-)opens this same SQLite file in its own
    # lifespan-owned engine/session.
    _seed_arr_instance(settings)
    engine = create_engine_from_settings(settings)
    upgrade_to_head(settings)
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
        arr_transport=_arr_ok_transport(),
        disk_usage=_ample_free_space(),
        update_check_transport=_offline_update_check_transport(),
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
    # Seed one Arr instance too -- otherwise the COL-77 check's own first-tick
    # failing transition would make its own (unrelated) notification call.
    _seed_arr_instance(settings)
    engine = create_engine_from_settings(settings)
    upgrade_to_head(settings)
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
        arr_transport=_arr_ok_transport(),
        disk_usage=_ample_free_space(),
        update_check_transport=_offline_update_check_transport(),
    )

    with TestClient(app):
        pass

    assert seen == []
