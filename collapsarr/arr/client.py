"""HTTP client for validating connectivity to a Sonarr/Radarr instance.

Sonarr and Radarr expose an identical ``/api/v3/system/status`` endpoint
(authenticated via the ``X-Api-Key`` header) that returns instance metadata
including its running version. :func:`check_connectivity` calls it and
reports success/failure without ever raising, so callers can persist the
result unconditionally.

Tests inject a ``transport`` (``httpx.MockTransport``) built from recorded
fixture responses instead of making real network calls.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

_STATUS_PATH = "/api/v3/system/status"
_DEFAULT_TIMEOUT = 10.0
_ERROR_BODY_LIMIT = 500


@dataclass(frozen=True, slots=True)
class ConnectivityResult:
    """Outcome of a connectivity/version check against an Arr instance."""

    ok: bool
    version: str | None = None
    error: str | None = None


def check_connectivity(
    base_url: str,
    api_key: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> ConnectivityResult:
    """Call the instance's system/status endpoint and report the result.

    Never raises: network errors, timeouts, non-2xx responses, and malformed
    payloads are all captured as a failed :class:`ConnectivityResult` so the
    service layer can persist success/failure state without a try/except.
    """
    url = f"{base_url.rstrip('/')}{_STATUS_PATH}"
    if transport is not None:
        client = httpx.Client(timeout=timeout, transport=transport)
    else:
        client = httpx.Client(timeout=timeout)

    try:
        with client:
            response = client.get(url, headers={"X-Api-Key": api_key})
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code}: {exc.response.text}"[:_ERROR_BODY_LIMIT]
        return ConnectivityResult(ok=False, error=detail)
    except httpx.HTTPError as exc:
        return ConnectivityResult(ok=False, error=str(exc))

    try:
        payload = response.json()
    except ValueError:
        return ConnectivityResult(ok=False, error="Invalid JSON in status response")

    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version:
        return ConnectivityResult(ok=False, error="Status response missing 'version' field")

    return ConnectivityResult(ok=True, version=version)
