"""Tests for the arr "on import"/"on upgrade" webhook handling (COL-14).

Covers both layers: pure payload parsing/resolution in
:mod:`collapsarr.arr.webhooks`, and the ``POST /api/webhook/arr/{instance_id}``
endpoint wired up in :mod:`collapsarr.main` -- instance lookup, path
resolution via ``RemotePathMapping``, invocation of the pluggable "file
ready" hook, and 4xx handling for malformed payloads. Fixtures under
``tests/fixtures/arr/`` are hand-built to match the real Sonarr/Radarr
webhook schema (``eventType`` plus nested ``series``/``episodeFile`` or
``movie``/``movieFile``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.arr.models import ArrInstance, InstanceType, RemotePathMapping
from collapsarr.arr.webhooks import (
    ResolvedWebhookFile,
    WebhookFile,
    WebhookValidationError,
    default_on_file_ready_hook,
    parse_radarr_webhook,
    parse_sonarr_webhook,
    parse_webhook_payload,
    resolve_webhook_file,
)
from collapsarr.config import Settings
from collapsarr.main import create_app
from collapsarr.settings.service import get_global_settings

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arr"


def _auth_headers(client: TestClient) -> dict[str, str]:
    """The API-key header for the app's auto-generated key (COL-26).

    The ``/api/webhook`` route is API-key protected like every other ``/api``
    route, so the endpoint tests must present the key the app minted on first
    run. Read it back through the app's own session factory.
    """
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory
    with session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _load_fixture(name: str) -> dict[str, Any]:
    payload = json.loads((FIXTURES_DIR / name).read_text())
    assert isinstance(payload, dict)
    return payload


# --- payload parsing ---------------------------------------------------------


def test_parse_sonarr_webhook_on_import() -> None:
    """A Sonarr "Download" event (fresh import) parses to a WebhookFile."""
    payload = _load_fixture("sonarr_webhook_on_import.json")

    file = parse_sonarr_webhook(payload)

    assert file == WebhookFile(
        media_title="Breaking Bad",
        file_path="/tv/Breaking Bad/Season 01/Breaking Bad - S01E01 - Pilot.mkv",
        is_upgrade=False,
        source_file_id=101,
    )


def test_parse_sonarr_webhook_on_upgrade() -> None:
    """A Sonarr "Download" event with isUpgrade=true is flagged as an upgrade."""
    payload = _load_fixture("sonarr_webhook_on_upgrade.json")

    file = parse_sonarr_webhook(payload)

    assert file is not None
    assert file.is_upgrade is True
    assert file.file_path.endswith("Cats in the Bag.mkv")


def test_parse_sonarr_webhook_ignores_non_download_event() -> None:
    """A valid but non-import event (e.g. Sonarr's "Test" button) yields None."""
    payload = _load_fixture("sonarr_webhook_test.json")

    assert parse_sonarr_webhook(payload) is None


def test_parse_sonarr_webhook_missing_event_type_is_malformed() -> None:
    with pytest.raises(WebhookValidationError):
        parse_sonarr_webhook({"series": {"title": "X"}})


def test_parse_sonarr_webhook_download_missing_series_is_malformed() -> None:
    with pytest.raises(WebhookValidationError):
        parse_sonarr_webhook({"eventType": "Download", "episodeFile": {"path": "/x"}})


def test_parse_sonarr_webhook_download_missing_episode_file_is_malformed() -> None:
    with pytest.raises(WebhookValidationError):
        parse_sonarr_webhook({"eventType": "Download", "series": {"title": "X"}})


def test_parse_radarr_webhook_on_import() -> None:
    """A Radarr "Download" event (fresh import) parses to a WebhookFile."""
    payload = _load_fixture("radarr_webhook_on_import.json")

    file = parse_radarr_webhook(payload)

    assert file == WebhookFile(
        media_title="Interstellar",
        file_path="/movies/Interstellar (2014)/Interstellar (2014) Bluray-1080p.mkv",
        is_upgrade=False,
        source_file_id=501,
    )


def test_parse_radarr_webhook_on_upgrade() -> None:
    """A Radarr "Download" event with isUpgrade=true is flagged as an upgrade."""
    payload = _load_fixture("radarr_webhook_on_upgrade.json")

    file = parse_radarr_webhook(payload)

    assert file is not None
    assert file.is_upgrade is True
    assert file.media_title == "Silent Film"


def test_parse_radarr_webhook_download_missing_movie_file_is_malformed() -> None:
    with pytest.raises(WebhookValidationError):
        parse_radarr_webhook({"eventType": "Download", "movie": {"title": "X"}})


def test_parse_webhook_payload_dispatches_by_instance_type() -> None:
    """parse_webhook_payload picks the Sonarr/Radarr parser from instance type alone."""
    sonarr_file = parse_webhook_payload(
        InstanceType.SONARR, _load_fixture("sonarr_webhook_on_import.json")
    )
    radarr_file = parse_webhook_payload(
        InstanceType.RADARR, _load_fixture("radarr_webhook_on_import.json")
    )

    assert sonarr_file is not None
    assert sonarr_file.media_title == "Breaking Bad"
    assert radarr_file is not None
    assert radarr_file.media_title == "Interstellar"


# --- path resolution ----------------------------------------------------------


def test_resolve_webhook_file_applies_path_mapping() -> None:
    instance = ArrInstance(
        id=7,
        name="Main Sonarr",
        type=InstanceType.SONARR,
        base_url="http://sonarr.local:8989",
        api_key="key",
    )
    mapping = RemotePathMapping(
        id=1, instance_id=7, remote_prefix="/tv", local_prefix="/mnt/media/tv", order=0
    )
    raw = WebhookFile(
        media_title="Breaking Bad",
        file_path="/tv/Breaking Bad/Season 01/Pilot.mkv",
        is_upgrade=False,
        source_file_id=101,
    )

    resolved = resolve_webhook_file(instance, raw, [mapping])

    assert resolved == ResolvedWebhookFile(
        instance_id=7,
        instance_name="Main Sonarr",
        media_title="Breaking Bad",
        file_path="/mnt/media/tv/Breaking Bad/Season 01/Pilot.mkv",
        is_upgrade=False,
        source_file_id=101,
    )


def test_resolve_webhook_file_passes_through_when_no_mapping_matches() -> None:
    instance = ArrInstance(
        id=9,
        name="Main Radarr",
        type=InstanceType.RADARR,
        base_url="http://radarr.local:7878",
        api_key="key",
    )
    raw = WebhookFile(
        media_title="Interstellar", file_path="/movies/Interstellar/file.mkv", is_upgrade=False
    )

    resolved = resolve_webhook_file(instance, raw, [])

    assert resolved.file_path == "/movies/Interstellar/file.mkv"


def test_default_on_file_ready_hook_logs(caplog: pytest.LogCaptureFixture) -> None:
    """The stub hook logs the resolved file rather than raising or doing nothing silently."""
    file = ResolvedWebhookFile(
        instance_id=1,
        instance_name="Sonarr",
        media_title="Breaking Bad",
        file_path="/mnt/media/tv/Breaking Bad/file.mkv",
        is_upgrade=True,
        source_file_id=1,
    )

    with caplog.at_level("INFO"):
        default_on_file_ready_hook(file)

    assert "Breaking Bad" in caplog.text


# --- HTTP endpoint -------------------------------------------------------------


def _instance(session: Session, *, instance_type: InstanceType) -> ArrInstance:
    instance = ArrInstance(
        name="Test Instance",
        type=instance_type,
        base_url="http://arr.local:8989",
        api_key="key",
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)
    return instance


def test_webhook_endpoint_accepts_sonarr_import_and_returns_200(
    settings: Settings, session: Session
) -> None:
    instance = _instance(session, instance_type=InstanceType.SONARR)
    captured: list[ResolvedWebhookFile] = []

    app = create_app(settings=settings, on_file_ready=captured.append)
    with TestClient(app) as client:
        response = client.post(
            f"/api/webhook/arr/{instance.id}",
            json=_load_fixture("sonarr_webhook_on_import.json"),
            headers=_auth_headers(client),
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(captured) == 1
    assert captured[0].media_title == "Breaking Bad"
    assert captured[0].is_upgrade is False
    assert captured[0].instance_id == instance.id


def test_webhook_endpoint_accepts_radarr_upgrade_and_returns_200(
    settings: Settings, session: Session
) -> None:
    instance = _instance(session, instance_type=InstanceType.RADARR)
    captured: list[ResolvedWebhookFile] = []

    app = create_app(settings=settings, on_file_ready=captured.append)
    with TestClient(app) as client:
        response = client.post(
            f"/api/webhook/arr/{instance.id}",
            json=_load_fixture("radarr_webhook_on_upgrade.json"),
            headers=_auth_headers(client),
        )

    assert response.status_code == 200
    assert len(captured) == 1
    assert captured[0].is_upgrade is True
    assert captured[0].media_title == "Silent Film"


def test_webhook_endpoint_resolves_path_via_instance_mapping(
    settings: Settings, session: Session
) -> None:
    """The hook receives a path already translated through the instance's mapping."""
    instance = _instance(session, instance_type=InstanceType.SONARR)
    session.add(
        RemotePathMapping(
            instance_id=instance.id, remote_prefix="/tv", local_prefix="/mnt/media/tv", order=0
        )
    )
    session.commit()
    captured: list[ResolvedWebhookFile] = []

    app = create_app(settings=settings, on_file_ready=captured.append)
    with TestClient(app) as client:
        response = client.post(
            f"/api/webhook/arr/{instance.id}",
            json=_load_fixture("sonarr_webhook_on_import.json"),
            headers=_auth_headers(client),
        )

    assert response.status_code == 200
    assert captured[0].file_path == (
        "/mnt/media/tv/Breaking Bad/Season 01/Breaking Bad - S01E01 - Pilot.mkv"
    )


def test_webhook_endpoint_default_hook_does_not_raise_when_unset(
    settings: Settings, session: Session
) -> None:
    """No on_file_ready override -> falls back to the log-only stub hook."""
    instance = _instance(session, instance_type=InstanceType.SONARR)

    app = create_app(settings=settings)
    with TestClient(app) as client:
        response = client.post(
            f"/api/webhook/arr/{instance.id}",
            json=_load_fixture("sonarr_webhook_on_import.json"),
            headers=_auth_headers(client),
        )

    assert response.status_code == 200


def test_webhook_endpoint_ignores_non_download_event(
    settings: Settings, session: Session
) -> None:
    """A "Test" event returns 200 but does not invoke the file-ready hook."""
    instance = _instance(session, instance_type=InstanceType.SONARR)
    captured: list[ResolvedWebhookFile] = []

    app = create_app(settings=settings, on_file_ready=captured.append)
    with TestClient(app) as client:
        response = client.post(
            f"/api/webhook/arr/{instance.id}",
            json=_load_fixture("sonarr_webhook_test.json"),
            headers=_auth_headers(client),
        )

    assert response.status_code == 200
    assert captured == []


def test_webhook_endpoint_returns_404_for_unknown_instance(settings: Settings) -> None:
    app = create_app(settings=settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/webhook/arr/999",
            json=_load_fixture("sonarr_webhook_on_import.json"),
            headers=_auth_headers(client),
        )

    assert response.status_code == 404


def test_webhook_endpoint_rejects_malformed_download_payload(
    settings: Settings, session: Session
) -> None:
    """A Download event missing series/episodeFile is rejected, not 500'd."""
    instance = _instance(session, instance_type=InstanceType.SONARR)

    app = create_app(settings=settings)
    with TestClient(app) as client:
        response = client.post(
            f"/api/webhook/arr/{instance.id}",
            json={"eventType": "Download"},
            headers=_auth_headers(client),
        )

    assert response.status_code == 422


def test_webhook_endpoint_rejects_payload_missing_event_type(
    settings: Settings, session: Session
) -> None:
    instance = _instance(session, instance_type=InstanceType.SONARR)

    app = create_app(settings=settings)
    with TestClient(app) as client:
        response = client.post(
            f"/api/webhook/arr/{instance.id}",
            json={"foo": "bar"},
            headers=_auth_headers(client),
        )

    assert response.status_code == 422


def test_webhook_endpoint_rejects_non_object_body(settings: Settings, session: Session) -> None:
    instance = _instance(session, instance_type=InstanceType.SONARR)

    app = create_app(settings=settings)
    with TestClient(app) as client:
        response = client.post(
            f"/api/webhook/arr/{instance.id}",
            json=["not", "an", "object"],
            headers=_auth_headers(client),
        )

    assert response.status_code == 422


def test_webhook_endpoint_rejects_non_integer_instance_id(settings: Settings) -> None:
    app = create_app(settings=settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/webhook/arr/not-an-int",
            json=_load_fixture("sonarr_webhook_on_import.json"),
            headers=_auth_headers(client),
        )

    assert response.status_code == 422


def test_webhook_endpoint_does_not_crash_app_on_repeated_malformed_requests(
    settings: Settings, session: Session
) -> None:
    """The app stays healthy after rejecting a run of malformed webhook payloads."""
    instance = _instance(session, instance_type=InstanceType.SONARR)

    app = create_app(settings=settings)
    with TestClient(app) as client:
        for _ in range(3):
            response = client.post(
                f"/api/webhook/arr/{instance.id}",
                json={"bad": True},
                headers=_auth_headers(client),
            )
            assert response.status_code == 422

        health = client.get("/health")
        assert health.status_code == 200
