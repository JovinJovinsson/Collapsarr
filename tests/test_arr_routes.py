"""Contract tests for the instance & path-mapping REST endpoints (COL-27).

Covers request/response shape and the API-key-required behaviour (COL-26) for
every CRUD route under ``/api/instances``. Connectivity is not stubbed here --
the create/update handlers call the real service, which runs a connectivity
check against ``base_url``; the tests point instances at an unreachable local
port so the check fails fast and the row persists with ``status="error"``
(the persist-regardless contract of the service layer). Shape assertions do
not depend on that outcome.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.arr.models import ArrInstance, InstanceType, RemotePathMapping
from collapsarr.settings.service import get_global_settings, update_global_settings

# Nothing listens here, so the service's connectivity check fails immediately
# rather than blocking on a DNS/connect timeout.
UNREACHABLE_URL = "http://127.0.0.1:9"


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _create_instance(client: TestClient, *, name: str = "Main Sonarr") -> dict[str, Any]:
    response = client.post(
        "/api/instances",
        json={
            "name": name,
            "type": "sonarr",
            "base_url": UNREACHABLE_URL,
            "api_key": "secret-key",
        },
        headers=_auth_headers(client),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _create_mapping(client: TestClient, instance_id: int) -> dict[str, Any]:
    response = client.post(
        f"/api/instances/{instance_id}/path-mappings",
        json={"remote_prefix": "/tv", "local_prefix": "/mnt/media/tv", "order": 1},
        headers=_auth_headers(client),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


# --- instance CRUD shape ------------------------------------------------------


def test_create_instance_returns_201_with_full_shape(client: TestClient) -> None:
    body = _create_instance(client)

    assert body["id"] is not None
    assert body["name"] == "Main Sonarr"
    assert body["type"] == "sonarr"
    assert body["base_url"] == UNREACHABLE_URL
    assert body["api_key"] == "secret-key"
    # Connectivity was attempted and recorded (unreachable -> error).
    assert body["status"] == "error"
    assert "created_at" in body
    assert "updated_at" in body


def test_list_instances_returns_created_rows(client: TestClient) -> None:
    _create_instance(client, name="A")
    _create_instance(client, name="B")

    response = client.get("/api/instances", headers=_auth_headers(client))

    assert response.status_code == 200
    names = [row["name"] for row in response.json()]
    assert names == ["A", "B"]


def test_get_instance_returns_the_row(client: TestClient) -> None:
    created = _create_instance(client)

    response = client.get(f"/api/instances/{created['id']}", headers=_auth_headers(client))

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_unknown_instance_returns_404(client: TestClient) -> None:
    response = client.get("/api/instances/999", headers=_auth_headers(client))
    assert response.status_code == 404


def test_update_instance_changes_fields(client: TestClient) -> None:
    created = _create_instance(client)

    response = client.put(
        f"/api/instances/{created['id']}",
        json={"name": "Renamed"},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"


def test_update_unknown_instance_returns_404(client: TestClient) -> None:
    response = client.put(
        "/api/instances/999", json={"name": "x"}, headers=_auth_headers(client)
    )
    assert response.status_code == 404


def test_delete_instance_returns_204_and_is_gone(client: TestClient) -> None:
    created = _create_instance(client)

    delete = client.delete(f"/api/instances/{created['id']}", headers=_auth_headers(client))
    assert delete.status_code == 204

    follow_up = client.get(f"/api/instances/{created['id']}", headers=_auth_headers(client))
    assert follow_up.status_code == 404


def test_delete_unknown_instance_returns_404(client: TestClient) -> None:
    response = client.delete("/api/instances/999", headers=_auth_headers(client))
    assert response.status_code == 404


# --- path-mapping CRUD shape --------------------------------------------------


def test_create_path_mapping_returns_201_with_shape(client: TestClient) -> None:
    instance = _create_instance(client)

    body = _create_mapping(client, int(instance["id"]))

    assert body["id"] is not None
    assert body["instance_id"] == instance["id"]
    assert body["remote_prefix"] == "/tv"
    assert body["local_prefix"] == "/mnt/media/tv"
    assert body["order"] == 1


def test_create_path_mapping_under_unknown_instance_returns_404(client: TestClient) -> None:
    response = client.post(
        "/api/instances/999/path-mappings",
        json={"remote_prefix": "/tv", "local_prefix": "/mnt/tv"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 404


def test_list_path_mappings_returns_rows(client: TestClient) -> None:
    instance = _create_instance(client)
    _create_mapping(client, int(instance["id"]))

    response = client.get(
        f"/api/instances/{instance['id']}/path-mappings", headers=_auth_headers(client)
    )

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["remote_prefix"] == "/tv"


def test_get_path_mapping_returns_row(client: TestClient) -> None:
    instance = _create_instance(client)
    mapping = _create_mapping(client, int(instance["id"]))

    response = client.get(
        f"/api/instances/{instance['id']}/path-mappings/{mapping['id']}",
        headers=_auth_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["id"] == mapping["id"]


def test_get_path_mapping_scoped_to_instance(client: TestClient) -> None:
    """A mapping id under the wrong instance is a 404, not a cross-instance leak."""
    first = _create_instance(client, name="First")
    second = _create_instance(client, name="Second")
    mapping = _create_mapping(client, int(first["id"]))

    response = client.get(
        f"/api/instances/{second['id']}/path-mappings/{mapping['id']}",
        headers=_auth_headers(client),
    )

    assert response.status_code == 404


def test_update_path_mapping_changes_fields(client: TestClient) -> None:
    instance = _create_instance(client)
    mapping = _create_mapping(client, int(instance["id"]))

    response = client.put(
        f"/api/instances/{instance['id']}/path-mappings/{mapping['id']}",
        json={"local_prefix": "/data/tv"},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["local_prefix"] == "/data/tv"
    assert response.json()["remote_prefix"] == "/tv"  # untouched


def test_update_unknown_path_mapping_returns_404(client: TestClient) -> None:
    instance = _create_instance(client)

    response = client.put(
        f"/api/instances/{instance['id']}/path-mappings/999",
        json={"local_prefix": "/x"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 404


def test_delete_path_mapping_returns_204_and_is_gone(client: TestClient) -> None:
    instance = _create_instance(client)
    mapping = _create_mapping(client, int(instance["id"]))

    delete = client.delete(
        f"/api/instances/{instance['id']}/path-mappings/{mapping['id']}",
        headers=_auth_headers(client),
    )
    assert delete.status_code == 204

    follow_up = client.get(
        f"/api/instances/{instance['id']}/path-mappings/{mapping['id']}",
        headers=_auth_headers(client),
    )
    assert follow_up.status_code == 404


def test_delete_unknown_path_mapping_returns_404(client: TestClient) -> None:
    instance = _create_instance(client)

    response = client.delete(
        f"/api/instances/{instance['id']}/path-mappings/999",
        headers=_auth_headers(client),
    )
    assert response.status_code == 404


# --- auth-required behaviour per endpoint -------------------------------------


def _seed_ids(session: Session) -> tuple[int, int]:
    """Persist an instance + mapping directly, returning their ids."""
    instance = ArrInstance(
        name="Seeded", type=InstanceType.SONARR, base_url=UNREACHABLE_URL, api_key="k"
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)
    mapping = RemotePathMapping(
        instance_id=instance.id, remote_prefix="/tv", local_prefix="/mnt/tv", order=0
    )
    session.add(mapping)
    session.commit()
    session.refresh(mapping)
    return instance.id, mapping.id


def test_every_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    instance_id, mapping_id = _seed_ids(session)
    update_global_settings(session, ui_auth_enabled=True)

    base = "/api/instances"
    requests = [
        ("get", base),
        ("post", base),
        ("get", f"{base}/{instance_id}"),
        ("put", f"{base}/{instance_id}"),
        ("delete", f"{base}/{instance_id}"),
        ("get", f"{base}/{instance_id}/path-mappings"),
        ("post", f"{base}/{instance_id}/path-mappings"),
        ("get", f"{base}/{instance_id}/path-mappings/{mapping_id}"),
        ("put", f"{base}/{instance_id}/path-mappings/{mapping_id}"),
        ("delete", f"{base}/{instance_id}/path-mappings/{mapping_id}"),
    ]

    for method, url in requests:
        response = client.request(method, url, json={})
        assert response.status_code == 401, f"{method.upper()} {url} was not gated"
