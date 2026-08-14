"""Tests for the SHA256SUMS checksum client (COL-230).

Mirrors :mod:`collapsarr.update_check.client`/:mod:`collapsarr.ffmpeg_download.
client`'s own test idiom: a ``transport`` (``httpx.MockTransport``) stands in
for the real network call, and :func:`parse_sha256sums`/
:func:`resolve_platform_arch`'s pure branches are exercised directly with no
network involved at all. Fixture content matches the real ``SHA256SUMS``
format ``release.yml`` publishes since COL-227 (``sha256sum -- * >
SHA256SUMS``): a 64-hex-char digest, two spaces, then the filename.
"""

from __future__ import annotations

import hashlib
import platform

import httpx
import pytest

from collapsarr.self_update.client import (
    SelfUpdateChecksumResult,
    fetch_checksum_entry,
    fetch_sha256sums,
    parse_sha256sums,
    resolve_asset_filename,
    resolve_checksum_entry,
    resolve_platform_arch,
)

_VERSION = "1.2.3"

_LINUX_AMD64_DIGEST = hashlib.sha256(b"linux amd64 archive").hexdigest()
_LINUX_ARM64_DIGEST = hashlib.sha256(b"linux arm64 archive").hexdigest()
_MACOS_AMD64_DIGEST = hashlib.sha256(b"macos amd64 archive").hexdigest()
_MACOS_ARM64_DIGEST = hashlib.sha256(b"macos arm64 archive").hexdigest()
_WINDOWS_AMD64_DIGEST = hashlib.sha256(b"windows amd64 archive").hexdigest()

#: A realistic multi-entry SHA256SUMS file, matching release.yml's
#: `sha256sum -- * > SHA256SUMS` output exactly (COL-227): text mode, two
#: spaces between the digest and the filename.
_SHA256SUMS_CONTENT = (
    f"{_LINUX_AMD64_DIGEST}  collapsarr-{_VERSION}-linux-amd64.tar.gz\n"
    f"{_LINUX_ARM64_DIGEST}  collapsarr-{_VERSION}-linux-arm64.tar.gz\n"
    f"{_MACOS_AMD64_DIGEST}  collapsarr-{_VERSION}-macos-amd64.tar.gz\n"
    f"{_MACOS_ARM64_DIGEST}  collapsarr-{_VERSION}-macos-arm64.tar.gz\n"
    f"{_WINDOWS_AMD64_DIGEST}  collapsarr-{_VERSION}-windows-amd64.zip\n"
)


# --------------------------------------------------------------------------- #
# parse_sha256sums
# --------------------------------------------------------------------------- #


def test_parse_sha256sums_parses_every_entry() -> None:
    entries = parse_sha256sums(_SHA256SUMS_CONTENT)

    assert entries == {
        f"collapsarr-{_VERSION}-linux-amd64.tar.gz": _LINUX_AMD64_DIGEST,
        f"collapsarr-{_VERSION}-linux-arm64.tar.gz": _LINUX_ARM64_DIGEST,
        f"collapsarr-{_VERSION}-macos-amd64.tar.gz": _MACOS_AMD64_DIGEST,
        f"collapsarr-{_VERSION}-macos-arm64.tar.gz": _MACOS_ARM64_DIGEST,
        f"collapsarr-{_VERSION}-windows-amd64.zip": _WINDOWS_AMD64_DIGEST,
    }


def test_parse_sha256sums_lowercases_the_digest() -> None:
    content = f"{_LINUX_AMD64_DIGEST.upper()}  collapsarr-{_VERSION}-linux-amd64.tar.gz\n"

    entries = parse_sha256sums(content)

    assert entries[f"collapsarr-{_VERSION}-linux-amd64.tar.gz"] == _LINUX_AMD64_DIGEST.lower()


def test_parse_sha256sums_accepts_the_binary_mode_asterisk_marker() -> None:
    content = f"{_LINUX_AMD64_DIGEST} *collapsarr-{_VERSION}-linux-amd64.tar.gz\n"

    entries = parse_sha256sums(content)

    assert entries == {f"collapsarr-{_VERSION}-linux-amd64.tar.gz": _LINUX_AMD64_DIGEST}


def test_parse_sha256sums_skips_blank_lines() -> None:
    content = f"\n\n{_LINUX_AMD64_DIGEST}  collapsarr-{_VERSION}-linux-amd64.tar.gz\n\n"

    entries = parse_sha256sums(content)

    assert entries == {f"collapsarr-{_VERSION}-linux-amd64.tar.gz": _LINUX_AMD64_DIGEST}


def test_parse_sha256sums_skips_malformed_lines_without_raising() -> None:
    content = (
        "this is not a checksum line at all\n"
        f"{_LINUX_AMD64_DIGEST}  collapsarr-{_VERSION}-linux-amd64.tar.gz\n"
        "not-hex-at-all-but-64-chars-long-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  bad.tar.gz\n"
    )

    entries = parse_sha256sums(content)

    assert entries == {f"collapsarr-{_VERSION}-linux-amd64.tar.gz": _LINUX_AMD64_DIGEST}


def test_parse_sha256sums_of_empty_content_is_empty() -> None:
    assert parse_sha256sums("") == {}


# --------------------------------------------------------------------------- #
# resolve_asset_filename
# --------------------------------------------------------------------------- #


def test_resolve_asset_filename_linux_is_a_tar_gz() -> None:
    filename = resolve_asset_filename("1.2.3", "linux", "amd64")
    assert filename == "collapsarr-1.2.3-linux-amd64.tar.gz"


def test_resolve_asset_filename_macos_is_a_tar_gz() -> None:
    filename = resolve_asset_filename("1.2.3", "macos", "arm64")
    assert filename == "collapsarr-1.2.3-macos-arm64.tar.gz"


def test_resolve_asset_filename_windows_is_a_zip() -> None:
    filename = resolve_asset_filename("1.2.3", "windows", "amd64")
    assert filename == "collapsarr-1.2.3-windows-amd64.zip"


# --------------------------------------------------------------------------- #
# resolve_platform_arch (mirrors test_ffmpeg_download_service.py's own
# resolve_platform_arch coverage for the identically-shaped mapping)
# --------------------------------------------------------------------------- #


def test_resolve_platform_arch_maps_linux_amd64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert resolve_platform_arch() == ("linux", "amd64")


def test_resolve_platform_arch_maps_linux_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    assert resolve_platform_arch() == ("linux", "arm64")


def test_resolve_platform_arch_maps_macos_amd64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert resolve_platform_arch() == ("macos", "amd64")


def test_resolve_platform_arch_maps_macos_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    assert resolve_platform_arch() == ("macos", "arm64")


def test_resolve_platform_arch_maps_windows_amd64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    assert resolve_platform_arch() == ("windows", "amd64")


def test_resolve_platform_arch_returns_none_for_an_unsupported_os(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert resolve_platform_arch() is None


def test_resolve_platform_arch_returns_none_for_an_unsupported_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "i386")
    assert resolve_platform_arch() is None


# --------------------------------------------------------------------------- #
# resolve_checksum_entry (pure: parse + resolve, no network)
# --------------------------------------------------------------------------- #


def test_resolve_checksum_entry_resolves_the_matching_platform_arch_entry() -> None:
    result = resolve_checksum_entry(_SHA256SUMS_CONTENT, _VERSION, "linux", "amd64")

    assert isinstance(result, SelfUpdateChecksumResult)
    assert result.ok is True
    assert result.filename == f"collapsarr-{_VERSION}-linux-amd64.tar.gz"
    assert result.sha256 == _LINUX_AMD64_DIGEST
    assert result.error is None


def test_resolve_checksum_entry_resolves_the_windows_zip_entry() -> None:
    result = resolve_checksum_entry(_SHA256SUMS_CONTENT, _VERSION, "windows", "amd64")

    assert result.ok is True
    assert result.filename == f"collapsarr-{_VERSION}-windows-amd64.zip"
    assert result.sha256 == _WINDOWS_AMD64_DIGEST


def test_resolve_checksum_entry_fails_when_no_entry_matches() -> None:
    result = resolve_checksum_entry(_SHA256SUMS_CONTENT, "9.9.9", "linux", "amd64")

    assert result.ok is False
    assert result.filename is None
    assert result.sha256 is None
    assert result.error is not None
    assert "collapsarr-9.9.9-linux-amd64.tar.gz" in result.error


def test_resolve_checksum_entry_fails_on_empty_content() -> None:
    result = resolve_checksum_entry("", _VERSION, "linux", "amd64")

    assert result.ok is False
    assert result.error is not None


# --------------------------------------------------------------------------- #
# fetch_sha256sums / fetch_checksum_entry (transport-level, no real network)
# --------------------------------------------------------------------------- #

_URL = "https://example.invalid/SHA256SUMS"


def test_fetch_sha256sums_returns_the_response_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SHA256SUMS_CONTENT)

    content, error = fetch_sha256sums(_URL, transport=httpx.MockTransport(handler))

    assert content == _SHA256SUMS_CONTENT
    assert error is None


def test_fetch_sha256sums_requests_the_given_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=_SHA256SUMS_CONTENT)

    fetch_sha256sums(_URL, transport=httpx.MockTransport(handler))

    assert len(seen) == 1
    assert str(seen[0].url) == _URL


def test_fetch_sha256sums_404_returns_a_clear_error_without_raising() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    content, error = fetch_sha256sums(_URL, transport=httpx.MockTransport(handler))

    assert content is None
    assert error is not None
    assert "404" in error


def test_fetch_sha256sums_network_error_returns_a_clear_error_without_raising() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    content, error = fetch_sha256sums(_URL, transport=httpx.MockTransport(handler))

    assert content is None
    assert error is not None


def test_fetch_sha256sums_timeout_returns_a_clear_error_without_raising() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    content, error = fetch_sha256sums(_URL, transport=httpx.MockTransport(handler))

    assert content is None
    assert error is not None


def test_fetch_checksum_entry_fetches_parses_and_resolves() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SHA256SUMS_CONTENT)

    result = fetch_checksum_entry(
        _URL, _VERSION, "macos", "arm64", transport=httpx.MockTransport(handler)
    )

    assert result.ok is True
    assert result.filename == f"collapsarr-{_VERSION}-macos-arm64.tar.gz"
    assert result.sha256 == _MACOS_ARM64_DIGEST


def test_fetch_checksum_entry_surfaces_a_fetch_failure_without_raising() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    result = fetch_checksum_entry(
        _URL, _VERSION, "linux", "amd64", transport=httpx.MockTransport(handler)
    )

    assert result.ok is False
    assert result.error is not None
    assert "500" in result.error
