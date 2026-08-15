"""Tests for the FFmpeg download+verify client (COL-217).

Mirrors :mod:`collapsarr.update_check.client`'s own test idiom (COL-86): a
``transport`` (``httpx.MockTransport``) stands in for the real network
call -- success, 404, checksum-mismatch, and network-error cases, no real
network access anywhere in this file.
"""

from __future__ import annotations

import hashlib

import httpx

from collapsarr.ffmpeg_download.client import (
    FfmpegDownloadResult,
    download_and_verify,
    download_manifest_entry,
)
from collapsarr.ffmpeg_download.manifest import ManifestEntry

_CONTENT = b"pretend this is an ffmpeg archive's bytes"
_CONTENT_SHA256 = hashlib.sha256(_CONTENT).hexdigest()
_URL = "https://example.invalid/ffmpeg-archive.tar.xz"


def _transport(handler: object) -> httpx.MockTransport:
    assert callable(handler)
    return httpx.MockTransport(handler)


def test_successful_download_verifies_matching_checksum() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_CONTENT)

    result = download_and_verify(_URL, _CONTENT_SHA256, transport=_transport(handler))

    assert isinstance(result, FfmpegDownloadResult)
    assert result.ok is True
    assert result.content == _CONTENT
    assert result.error is None


def test_checksum_comparison_is_case_insensitive() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_CONTENT)

    result = download_and_verify(_URL, _CONTENT_SHA256.upper(), transport=_transport(handler))

    assert result.ok is True


def test_requests_the_given_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_CONTENT)

    download_and_verify(_URL, _CONTENT_SHA256, transport=_transport(handler))

    assert len(seen) == 1
    assert str(seen[0].url) == _URL


def test_checksum_mismatch_returns_a_clear_error_without_raising() -> None:
    wrong_sha256 = hashlib.sha256(b"different bytes entirely").hexdigest()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_CONTENT)

    result = download_and_verify(_URL, wrong_sha256, transport=_transport(handler))

    assert result.ok is False
    assert result.content is None
    assert result.error is not None
    assert "sha-256" in result.error.lower() or "mismatch" in result.error.lower()
    assert wrong_sha256 in result.error
    assert _CONTENT_SHA256 in result.error


def test_404_response_returns_a_clear_error_without_raising() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    result = download_and_verify(_URL, _CONTENT_SHA256, transport=_transport(handler))

    assert result.ok is False
    assert result.content is None
    assert result.error is not None
    assert "404" in result.error


def test_network_error_returns_a_clear_error_without_raising() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = download_and_verify(_URL, _CONTENT_SHA256, transport=_transport(handler))

    assert result.ok is False
    assert result.content is None
    assert result.error is not None


def test_timeout_returns_a_clear_error_without_raising() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    result = download_and_verify(_URL, _CONTENT_SHA256, transport=_transport(handler))

    assert result.ok is False
    assert result.error is not None


def test_download_manifest_entry_delegates_to_download_and_verify() -> None:
    entry = ManifestEntry(
        platform="linux", arch="amd64", version="8.1.2", url=_URL, sha256=_CONTENT_SHA256
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == _URL
        return httpx.Response(200, content=_CONTENT)

    result = download_manifest_entry(entry, transport=_transport(handler))

    assert result.ok is True
    assert result.content == _CONTENT


def test_follows_redirects() -> None:
    # Both real providers 302 the manifest URL: GitHub Releases assets
    # redirect to a signed S3 URL, and evermeet.cx redirects to its current
    # mirror host. A client that doesn't follow redirects would treat every
    # real download as a failure -- this pins that behavior at the unit
    # level (the real-network case is covered end to end by
    # collapsarr/ffmpeg_download/verify_manifest.py in CI).
    redirect_url = "https://example.invalid/redirected-archive.tar.xz"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _URL:
            return httpx.Response(302, headers={"Location": redirect_url})
        assert str(request.url) == redirect_url
        return httpx.Response(200, content=_CONTENT)

    result = download_and_verify(_URL, _CONTENT_SHA256, transport=_transport(handler))

    assert result.ok is True
    assert result.content == _CONTENT


def test_download_manifest_entry_surfaces_checksum_mismatch() -> None:
    entry = ManifestEntry(
        platform="macos",
        arch="arm64",
        version="9.0.1",
        url=_URL,
        sha256=hashlib.sha256(b"stale pin").hexdigest(),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_CONTENT)

    result = download_manifest_entry(entry, transport=_transport(handler))

    assert result.ok is False
    assert result.error is not None


# --------------------------------------------------------------------------- #
# `max_bytes` streaming guard (COL-222 design note -- see this module's
# docstring on `download_and_verify`): bounds peak memory for the
# user-triggered auto-download path against a compromised/misbehaving
# redirect target serving an oversized response, without changing the
# default (`max_bytes=None`) behaviour every case above already covers.
# --------------------------------------------------------------------------- #


def test_max_bytes_none_preserves_the_original_unbounded_behaviour() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_CONTENT)

    result = download_and_verify(
        _URL, _CONTENT_SHA256, transport=_transport(handler), max_bytes=None
    )

    assert result.ok is True
    assert result.content == _CONTENT


def test_response_within_max_bytes_still_succeeds() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_CONTENT)

    result = download_and_verify(
        _URL, _CONTENT_SHA256, transport=_transport(handler), max_bytes=len(_CONTENT) + 1
    )

    assert result.ok is True
    assert result.content == _CONTENT


def test_response_exceeding_max_bytes_is_aborted_without_raising() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_CONTENT)

    result = download_and_verify(
        _URL, _CONTENT_SHA256, transport=_transport(handler), max_bytes=len(_CONTENT) - 1
    )

    assert result.ok is False
    assert result.content is None
    assert result.error is not None
    assert "exceeded" in result.error.lower()


def test_max_bytes_still_follows_redirects() -> None:
    redirect_url = "https://example.invalid/redirected-archive.tar.xz"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _URL:
            return httpx.Response(302, headers={"Location": redirect_url})
        return httpx.Response(200, content=_CONTENT)

    result = download_and_verify(
        _URL, _CONTENT_SHA256, transport=_transport(handler), max_bytes=len(_CONTENT) + 1
    )

    assert result.ok is True
    assert result.content == _CONTENT


def test_max_bytes_reports_a_clear_error_on_a_non_2xx_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    result = download_and_verify(
        _URL, _CONTENT_SHA256, transport=_transport(handler), max_bytes=1024
    )

    assert result.ok is False
    assert result.content is None
    assert "404" in (result.error or "")


def test_download_manifest_entry_forwards_max_bytes() -> None:
    entry = ManifestEntry(
        platform="linux", arch="amd64", version="8.1.2", url=_URL, sha256=_CONTENT_SHA256
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_CONTENT)

    result = download_manifest_entry(
        entry, transport=_transport(handler), max_bytes=len(_CONTENT) - 1
    )

    assert result.ok is False
    assert result.error is not None
    assert "exceeded" in result.error.lower()
