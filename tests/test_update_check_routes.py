"""Contract tests for the Update Check REST endpoints (COL-87, dismiss/undismiss COL-89).

Covers ``GET /api/system/updates``: the API-gate behaviour (401 without a
key/session), the response shape (running version, latest version, changelog,
checked_at, update_available), and both the "up to date" and "update
available" states.

Also covers ``POST /api/system/updates/recheck``: the auth gate, that it runs
a real tick and returns the refreshed state, that a manual recheck is
reflected by a subsequent ``GET``, that it fires the same edge-triggered
notification a scheduled tick would, and that it never disturbs the
background scheduler's own periodic thread/timer -- the same guarantees
``tests/test_health_routes.py`` asserts for the health-checks recheck
endpoint.

``POST /api/system/updates/dismiss`` / ``.../undismiss`` (COL-89) round-trip
the singleton row's ``dismissed_at``, mirroring
``tests/test_health_routes.py``'s per-check dismiss/undismiss endpoint tests.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr import __version__
from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.main import create_app
from collapsarr.migrations import upgrade_to_head
from collapsarr.notify.service import update_notifier_config
from collapsarr.settings.models import UPDATE_CHANNEL_BETA
from collapsarr.settings.service import get_global_settings, update_global_settings
from collapsarr.update_check.comparison import running_version_tag


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _release_transport(tag: str, *, name: str = "", body: str = "") -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tag_name": tag,
                "name": name,
                "body": body,
                "published_at": "2026-08-01T00:00:00Z",
            },
        )

    return httpx.MockTransport(handler)


def _app_with_release(
    settings: Settings, tag: str, *, name: str = "", body: str = ""
) -> FastAPI:
    return create_app(
        settings=settings,
        update_check_transport=_release_transport(tag, name=name, body=body),
    )


def _prerelease_transport(tag: str, *, name: str = "", body: str = "") -> httpx.MockTransport:
    """A ``GET /releases`` transport whose only entry is a prerelease (COL-88)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"tag_name": tag, "prerelease": True, "name": name, "body": body}],
        )

    return httpx.MockTransport(handler)


def _app_with_prerelease(
    settings: Settings, tag: str, *, name: str = "", body: str = ""
) -> FastAPI:
    return create_app(
        settings=settings,
        update_check_transport=_prerelease_transport(tag, name=name, body=body),
    )


def _seed_update_channel(settings: Settings, channel: str) -> None:
    """Pre-seed ``update_channel`` before the app's lifespan runs its startup tick.

    Builds the schema and settings row directly (mirrors ``conftest.py``'s
    ``session`` fixture) rather than going through the app -- setting the
    channel via ``PUT /api/settings`` after startup would be one tick too
    late for tests asserting on the very first (lifespan) tick's result.
    """
    upgrade_to_head(settings)
    engine = create_engine_from_settings(settings)
    try:
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            update_global_settings(session, update_channel=channel)
    finally:
        engine.dispose()


def _combined_transport(*, stable_tag: str, beta_tag: str) -> httpx.MockTransport:
    """Serves both ``releases/latest`` (stable) and ``releases`` (beta) consistently (COL-88).

    Real GitHub answers the same regardless of when it's called; this mirrors
    that so a single running app/scheduler can be rechecked *after* switching
    channels, without swapping the injected transport out from under it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": stable_tag, "name": stable_tag})
        return httpx.Response(
            200, json=[{"tag_name": beta_tag, "prerelease": True, "name": beta_tag}]
        )

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #


def test_get_updates_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/system/updates").status_code == 401


def test_recheck_requires_authentication(client: TestClient) -> None:
    assert client.post("/api/system/updates/recheck").status_code == 401


def test_dismiss_requires_authentication(client: TestClient) -> None:
    assert client.post("/api/system/updates/dismiss").status_code == 401


def test_undismiss_requires_authentication(client: TestClient) -> None:
    assert client.post("/api/system/updates/undismiss").status_code == 401


# --------------------------------------------------------------------------- #
# GET /api/system/updates -- shape + states
# --------------------------------------------------------------------------- #


def test_get_updates_before_any_successful_fetch(client: TestClient) -> None:
    """The shared `client` fixture's startup tick uses an offline transport
    (see `conftest.py`'s `_offline_update_check_transport`), so `checked_at`
    is set (a tick ran) but no release data was ever fetched successfully."""
    headers = _auth_headers(client)

    response = client.get("/api/system/updates", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["running_version"] == __version__
    assert body["latest_version"] is None
    assert body["latest_version_label"] is None
    assert body["changelog"] is None
    assert body["checked_at"] is not None
    # No confirmed match -- "unknown" reports as an update possibly being
    # available rather than falsely claiming the instance is current.
    assert body["update_available"] is True


def test_get_updates_reports_up_to_date_when_latest_tag_matches_running_version(
    settings: Settings,
) -> None:
    app = _app_with_release(settings, running_version_tag(__version__), name="Current release")
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.get("/api/system/updates", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["latest_version"] == running_version_tag(__version__)
        assert body["latest_version_label"] == "Current release"
        assert body["update_available"] is False


def test_get_updates_reports_update_available_when_latest_tag_differs(
    settings: Settings,
) -> None:
    app = _app_with_release(
        settings, "v999.0.0", name="Big new release", body="- shiny new stuff"
    )
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.get("/api/system/updates", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["running_version"] == __version__
        assert body["latest_version"] == "v999.0.0"
        assert body["latest_version_label"] == "Big new release"
        assert body["changelog"] == "- shiny new stuff"
        assert body["update_available"] is True


# --------------------------------------------------------------------------- #
# POST /api/system/updates/recheck
# --------------------------------------------------------------------------- #


def test_recheck_runs_a_tick_and_returns_the_refreshed_state(settings: Settings) -> None:
    # Startup tick sees no release yet (default offline-ish transport isn't
    # used here -- build the app with an explicit release transport so the
    # *recheck* is what actually surfaces it, distinguishing "state before"
    # from "state after").
    app = _app_with_release(settings, "v2.0.0", name="v2.0.0", body="notes")
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.post("/api/system/updates/recheck", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["latest_version"] == "v2.0.0"
        assert body["update_available"] is True

        # And it's live, not cached: a subsequent GET reflects the same,
        # freshly persisted state.
        get_body = test_client.get("/api/system/updates", headers=headers).json()
        assert get_body["latest_version"] == "v2.0.0"
        assert get_body["checked_at"] == body["checked_at"]



# --------------------------------------------------------------------------- #
# Beta channel (COL-88)
# --------------------------------------------------------------------------- #


def test_get_updates_reports_up_to_date_on_beta_channel_when_sha_matches(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.update_check.routes.__version__", "0.1.0+beta.abc1234")
    _seed_update_channel(settings, UPDATE_CHANNEL_BETA)
    app = _app_with_prerelease(settings, "beta-abc1234", name="Beta build abc1234")
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.get("/api/system/updates", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["latest_version"] == "beta-abc1234"
        assert body["update_available"] is False


def test_get_updates_reports_update_available_on_beta_channel_when_sha_differs(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("collapsarr.update_check.routes.__version__", "0.1.0+beta.abc1234")
    _seed_update_channel(settings, UPDATE_CHANNEL_BETA)
    app = _app_with_prerelease(settings, "beta-def5678", name="Beta build def5678")
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.get("/api/system/updates", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["latest_version"] == "beta-def5678"
        assert body["update_available"] is True


def test_get_updates_reports_update_available_on_beta_channel_for_a_stable_running_version(
    settings: Settings,
) -> None:
    """Documented edge case (COL-88's acceptance criteria): a stable build
    switched to the beta channel has no embedded SHA to compare, so it shows
    "update available" rather than being treated as a bug."""
    _seed_update_channel(settings, UPDATE_CHANNEL_BETA)
    app = _app_with_prerelease(settings, "beta-abc1234", name="Beta build abc1234")
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.get("/api/system/updates", headers=headers)

        assert response.status_code == 200
        assert response.json()["update_available"] is True


def test_recheck_after_switching_channel_reflects_the_new_channels_latest_release(
    settings: Settings,
) -> None:
    """COL-88: switching ``update_channel`` then calling recheck surfaces the
    newly selected channel's latest release -- not the previous channel's
    stale cached data -- on both the recheck response and a follow-up GET."""
    app = create_app(
        settings=settings,
        update_check_transport=_combined_transport(stable_tag="v1.0.0", beta_tag="beta-abc1234"),
    )
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)
        before = test_client.get("/api/system/updates", headers=headers).json()
        assert before["latest_version"] == "v1.0.0"

        switch = test_client.put(
            "/api/settings", json={"update_channel": "beta"}, headers=headers
        )
        assert switch.status_code == 200, switch.text

        response = test_client.post("/api/system/updates/recheck", headers=headers)

        assert response.status_code == 200
        assert response.json()["latest_version"] == "beta-abc1234"

        follow_up = test_client.get("/api/system/updates", headers=headers)
        assert follow_up.json()["latest_version"] == "beta-abc1234"


def test_recheck_does_not_disrupt_the_background_scheduler(settings: Settings) -> None:
    """A manual recheck is an extra, out-of-band tick, not a scheduler restart:
    it must not touch the periodic background thread or reset its timer."""
    app = create_app(
        settings=settings,
        enable_scheduler=True,
        update_check_transport=_release_transport("v1.0.0"),
    )
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)
        scheduler = app.state.update_check_scheduler
        thread_before = scheduler._thread

        response = test_client.post("/api/system/updates/recheck", headers=headers)

        assert response.status_code == 200
        # Same thread object, still alive and running -- the recheck never
        # touched the loop's `_thread` / `_stop` event.
        assert scheduler._thread is thread_before
        assert scheduler._thread is not None
        assert scheduler._thread.is_alive()


# --------------------------------------------------------------------------- #
# COL-89: POST /api/system/updates/dismiss and /undismiss
# --------------------------------------------------------------------------- #


def test_dismiss_sets_dismissed_at(settings: Settings) -> None:
    app = _app_with_release(settings, "v999.0.0")
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.post("/api/system/updates/dismiss", headers=headers)

        assert response.status_code == 200
        assert response.json()["dismissed_at"] is not None


def test_undismiss_clears_dismissed_at(settings: Settings) -> None:
    app = _app_with_release(settings, "v999.0.0")
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)
        test_client.post("/api/system/updates/dismiss", headers=headers)

        response = test_client.post("/api/system/updates/undismiss", headers=headers)

        assert response.status_code == 200
        assert response.json()["dismissed_at"] is None

        follow_up = test_client.get("/api/system/updates", headers=headers)
        assert follow_up.json()["dismissed_at"] is None


def test_a_new_recheck_result_clears_a_prior_dismissal(settings: Settings) -> None:
    """AC: a dismissed notice automatically reappears once an even newer
    version is published -- the same reconcile transition logic clears it."""
    app = create_app(
        settings=settings,
        update_check_transport=_combined_transport(stable_tag="v1.0.0", beta_tag="beta-abc1234"),
    )
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)
        test_client.post("/api/system/updates/dismiss", headers=headers)
        assert (
            test_client.get("/api/system/updates", headers=headers).json()["dismissed_at"]
            is not None
        )

        # A recheck against a transport whose stable tag genuinely changed.
        app2 = create_app(
            settings=settings, update_check_transport=_release_transport("v2.0.0")
        )
    with TestClient(app2) as test_client2:
        headers2 = _auth_headers(test_client2)
        response = test_client2.post("/api/system/updates/recheck", headers=headers2)

        assert response.status_code == 200
        assert response.json()["dismissed_at"] is None


# --------------------------------------------------------------------------- #
# COL-89: recheck fires the same edge-triggered notification a scheduled tick
# would -- same reconciliation path, not a separate mechanism.
# --------------------------------------------------------------------------- #


def test_recheck_fires_a_notification_when_the_latest_tag_is_new(settings: Settings) -> None:
    # `notify_transport` is shared by the Health Check Framework's own
    # transition notifications too (one notifier config, see
    # `collapsarr.main.create_app`'s docstring) -- the app's startup tick
    # notifies about the sandbox's default-failing health checks (e.g.
    # ffmpeg_missing) independently of the update-available event this test
    # cares about, so filter captured requests down to the `update_available`
    # event type rather than asserting on the raw request count.
    seen: list[httpx.Request] = []

    def notify_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    def update_available_requests() -> list[httpx.Request]:
        return [r for r in seen if r.content and b'"update_available"' in r.content]

    upgrade_to_head(settings)
    engine = create_engine_from_settings(settings)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        update_notifier_config(
            session, webhook_url="https://example.com/hook", webhook_enabled=True
        )
    engine.dispose()

    app = create_app(
        settings=settings,
        update_check_transport=_release_transport("v9.9.9"),
        notify_transport=httpx.MockTransport(notify_handler),
    )
    with TestClient(app) as test_client:
        headers = _auth_headers(test_client)

        response = test_client.post("/api/system/updates/recheck", headers=headers)

        assert response.status_code == 200
        # The startup tick already fired one (first-ever fetch, v9.9.9); this
        # recheck sees the same tag, so it must not fire a second one.
        assert len(update_available_requests()) == 1

        # Rechecking again against the same tag must not fire a second one.
        again = test_client.post("/api/system/updates/recheck", headers=headers)
        assert again.status_code == 200
        assert len(update_available_requests()) == 1
