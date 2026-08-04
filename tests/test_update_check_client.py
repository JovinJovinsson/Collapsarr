"""Tests for the GitHub Releases client (COL-86, beta channel COL-88).

Mirrors :mod:`collapsarr.arr.client`'s own test idiom (never raises; a
``transport`` (``httpx.MockTransport``) stands in for the real network call).
"""

from __future__ import annotations

import httpx
import pytest

from collapsarr.update_check.client import (
    GITHUB_REPO,
    fetch_latest_prerelease,
    fetch_latest_release,
)


def _transport(handler: object) -> httpx.MockTransport:
    assert callable(handler)
    return httpx.MockTransport(handler)


def test_requests_the_correct_repo_latest_release_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "tag_name": "v1.2.3",
                "name": "v1.2.3",
                "body": "changelog",
                "published_at": "2026-08-01T00:00:00Z",
            },
        )

    fetch_latest_release(transport=_transport(handler))

    assert len(seen) == 1
    assert str(seen[0].url) == f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def test_successful_fetch_parses_tag_name_and_label_and_changelog() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tag_name": "v1.2.3",
                "name": "Collapsarr v1.2.3",
                "body": "- fixed things",
                "published_at": "2026-08-01T12:30:00Z",
            },
        )

    result = fetch_latest_release(transport=_transport(handler))

    assert result.ok is True
    assert result.tag == "v1.2.3"
    assert result.name == "Collapsarr v1.2.3"
    assert result.body == "- fixed things"
    assert result.published_at is not None
    assert result.published_at.year == 2026
    assert result.error is None


def test_never_raises_on_connection_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = fetch_latest_release(transport=_transport(handler))

    assert result.ok is False
    assert result.tag is None
    assert result.error is not None


def test_never_raises_on_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    result = fetch_latest_release(transport=_transport(handler))

    assert result.ok is False
    assert "timed out" in (result.error or "").lower() or result.error is not None


def test_never_raises_on_non_2xx_status() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    result = fetch_latest_release(transport=_transport(handler))

    assert result.ok is False
    assert result.error is not None
    assert "404" in result.error


def test_never_raises_on_invalid_json() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})

    result = fetch_latest_release(transport=_transport(handler))

    assert result.ok is False
    assert result.error is not None


def test_never_raises_when_tag_name_is_missing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "no tag here"})

    result = fetch_latest_release(transport=_transport(handler))

    assert result.ok is False
    assert result.error is not None


def test_never_raises_on_unparsable_published_at() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"tag_name": "v1.2.3", "published_at": "not-a-date"}
        )

    result = fetch_latest_release(transport=_transport(handler))

    assert result.ok is False
    assert result.error is not None


@pytest.mark.parametrize("missing_field", ["name", "body"])
def test_missing_optional_fields_default_to_none(missing_field: str) -> None:
    payload = {"tag_name": "v1.2.3", "name": "v1.2.3", "body": "log"}
    del payload[missing_field]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    result = fetch_latest_release(transport=_transport(handler))

    assert result.ok is True
    assert getattr(result, missing_field) is None


# ---------------------------------------------------------------------------
# fetch_latest_prerelease (COL-88): beta channel.
# ---------------------------------------------------------------------------


def test_prerelease_requests_the_correct_repo_releases_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{"tag_name": "beta-v0.2.1.0007", "prerelease": True}])

    fetch_latest_prerelease(transport=_transport(handler))

    assert len(seen) == 1
    assert str(seen[0].url) == f"https://api.github.com/repos/{GITHUB_REPO}/releases"


def test_prerelease_picks_the_first_prerelease_entry() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"tag_name": "v2.0.0", "prerelease": False, "draft": False},
                {
                    "tag_name": "beta-v0.2.1.0007",
                    "prerelease": True,
                    "name": "Beta build 0.2.1.0007",
                },
                {
                    "tag_name": "beta-v0.2.1.0008",
                    "prerelease": True,
                    "name": "Beta build 0.2.1.0008",
                },
            ],
        )

    result = fetch_latest_prerelease(transport=_transport(handler))

    assert result.ok is True
    assert result.tag == "beta-v0.2.1.0007"
    assert result.name == "Beta build 0.2.1.0007"


def test_prerelease_never_raises_when_no_prerelease_entry_exists() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"tag_name": "v2.0.0", "prerelease": False}])

    result = fetch_latest_prerelease(transport=_transport(handler))

    assert result.ok is False
    assert result.error is not None


def test_prerelease_never_raises_on_an_empty_list() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    result = fetch_latest_prerelease(transport=_transport(handler))

    assert result.ok is False
    assert result.error is not None


def test_prerelease_never_raises_when_response_is_not_a_list() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tag_name": "v1.2.3"})

    result = fetch_latest_prerelease(transport=_transport(handler))

    assert result.ok is False
    assert result.error is not None


def test_prerelease_never_raises_on_connection_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = fetch_latest_prerelease(transport=_transport(handler))

    assert result.ok is False
    assert result.error is not None


def test_prerelease_never_raises_on_non_2xx_status() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    result = fetch_latest_prerelease(transport=_transport(handler))

    assert result.ok is False
    assert "404" in (result.error or "")


def test_prerelease_never_raises_when_tag_name_is_missing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"prerelease": True, "name": "no tag here"}])

    result = fetch_latest_prerelease(transport=_transport(handler))

    assert result.ok is False
    assert result.error is not None


def test_prerelease_parses_published_at_and_changelog() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "tag_name": "beta-v0.2.1.0007",
                    "prerelease": True,
                    "name": "Beta build 0.2.1.0007",
                    "body": "- experimental change",
                    "published_at": "2026-08-01T12:30:00Z",
                }
            ],
        )

    result = fetch_latest_prerelease(transport=_transport(handler))

    assert result.ok is True
    assert result.body == "- experimental change"
    assert result.published_at is not None
    assert result.published_at.year == 2026
