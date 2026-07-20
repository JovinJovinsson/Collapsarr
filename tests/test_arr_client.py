"""Tests for the Sonarr/Radarr connectivity check client.

Every case is driven by an ``httpx.MockTransport`` fed from recorded fixture
responses under ``tests/fixtures/arr/`` — no live network call is made.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from collapsarr.arr.client import ConnectivityResult, check_connectivity

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arr"


def _load_fixture(name: str) -> dict[str, object]:
    payload: dict[str, object] = json.loads((FIXTURES_DIR / name).read_text())
    return payload


def _transport_returning(status_code: int, payload: object) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


def test_sonarr_status_ok_reports_version() -> None:
    """A 200 response with the recorded Sonarr status payload is a success."""
    payload = _load_fixture("sonarr_status_ok.json")
    transport = _transport_returning(200, payload)

    result = check_connectivity("http://sonarr.local:8989", "sonarr-api-key", transport=transport)

    assert result == ConnectivityResult(ok=True, version="4.0.1.929", error=None)


def test_radarr_status_ok_reports_version() -> None:
    """A 200 response with the recorded Radarr status payload is a success."""
    payload = _load_fixture("radarr_status_ok.json")
    transport = _transport_returning(200, payload)

    result = check_connectivity("http://radarr.local:7878", "radarr-api-key", transport=transport)

    assert result == ConnectivityResult(ok=True, version="5.4.6.8723", error=None)


def test_request_carries_api_key_header_and_status_path() -> None:
    """The X-Api-Key header and the /api/v3/system/status path are used."""
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"version": "4.0.1.929"})

    transport = httpx.MockTransport(handler)

    check_connectivity("http://sonarr.local:8989/", "sonarr-api-key", transport=transport)

    request = seen["request"]
    assert request.url.path == "/api/v3/system/status"
    assert request.headers["X-Api-Key"] == "sonarr-api-key"


def test_unauthorized_response_is_reported_as_failure() -> None:
    """A 401 (bad API key) is a failure with the response captured in error."""
    payload = _load_fixture("unauthorized.json")
    transport = _transport_returning(401, payload)

    result = check_connectivity("http://sonarr.local:8989", "wrong-key", transport=transport)

    assert result.ok is False
    assert result.version is None
    assert result.error is not None
    assert "401" in result.error


def test_connection_error_is_reported_as_failure() -> None:
    """A transport-level failure (host unreachable) is a failure, not a raise."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    transport = httpx.MockTransport(handler)

    result = check_connectivity("http://unreachable.local:8989", "some-key", transport=transport)

    assert result.ok is False
    assert result.version is None
    assert result.error is not None


def test_timeout_is_reported_as_failure() -> None:
    """A timeout while connecting is a failure, not a raise."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Timed out", request=request)

    transport = httpx.MockTransport(handler)

    result = check_connectivity("http://slow.local:8989", "some-key", transport=transport)

    assert result.ok is False
    assert result.error is not None


def test_malformed_json_is_reported_as_failure() -> None:
    """A non-JSON body is a failure, not an unhandled exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    transport = httpx.MockTransport(handler)

    result = check_connectivity("http://sonarr.local:8989", "some-key", transport=transport)

    assert result.ok is False
    assert result.error is not None


@pytest.mark.parametrize("payload", [{}, {"version": 123}, {"version": ""}])
def test_missing_or_invalid_version_field_is_reported_as_failure(
    payload: dict[str, object],
) -> None:
    """A 200 response missing a usable ``version`` string is a failure."""
    transport = _transport_returning(200, payload)

    result = check_connectivity("http://sonarr.local:8989", "some-key", transport=transport)

    assert result.ok is False
    assert result.error is not None
