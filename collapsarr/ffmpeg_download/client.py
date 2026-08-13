"""HTTPS download + SHA-256 verification client for pinned FFmpeg builds (COL-217).

Mirrors :mod:`collapsarr.update_check.client`'s ``fetch_latest_release``
pattern exactly: an ``httpx.Client`` built with an injectable
``transport: httpx.BaseTransport | None = None`` (tests pass an
``httpx.MockTransport`` instead of making a real network call), and a
never-raising, ``ok``-flagged result dataclass -- connection errors,
timeouts, non-2xx responses, and (new here) a SHA-256 mismatch are all
captured as a failed :class:`FfmpegDownloadResult` with a short
human-readable ``error``, rather than propagating an exception.

Per ADR 0002 ("FFmpeg auto-download is opt-in, checksum-verified, and
version-pinned"): HTTPS + SHA-256 checksum only -- no GPG dependency, since
not every provider (evermeet.cx) publishes signatures.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

from .manifest import ManifestEntry

_DEFAULT_TIMEOUT = 30.0
_ERROR_BODY_LIMIT = 500


@dataclass(frozen=True, slots=True)
class FfmpegDownloadResult:
    """Outcome of downloading and SHA-256-verifying a manifest entry's archive.

    ``ok=False`` is the "no result" signal -- network error, timeout, non-2xx,
    or a checksum mismatch -- with ``error`` carrying a short human-readable
    reason for logging. On success, ``content`` holds the downloaded bytes
    (verified against ``expected_sha256``); the caller is responsible for
    writing them to disk / extracting the archive.
    """

    ok: bool
    content: bytes | None = None
    error: str | None = None


def download_and_verify(
    url: str,
    expected_sha256: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> FfmpegDownloadResult:
    """Download ``url`` and verify its SHA-256 digest against ``expected_sha256``.

    Never raises: every failure mode -- connection error, timeout, non-2xx, or
    a digest that doesn't match -- is captured as a failed
    :class:`FfmpegDownloadResult` instead of propagating, matching
    :func:`collapsarr.update_check.client.fetch_latest_release`'s contract.
    ``expected_sha256`` is compared case-insensitively (manifest entries are
    lowercase hex, but a caller-supplied value need not be).

    ``follow_redirects=True`` is required here (unlike
    ``update_check/client.py``'s JSON API calls): both providers' manifest
    URLs redirect -- GitHub Releases assets 302 to a signed S3 URL, and
    evermeet.cx 302s to its current mirror host -- so a client that doesn't
    follow redirects would treat every real download as a failure.
    """
    client = (
        httpx.Client(timeout=timeout, transport=transport, follow_redirects=True)
        if transport is not None
        else httpx.Client(timeout=timeout, follow_redirects=True)
    )

    try:
        with client:
            response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code}: {exc.response.text}"[:_ERROR_BODY_LIMIT]
        return FfmpegDownloadResult(ok=False, error=detail)
    except httpx.HTTPError as exc:
        return FfmpegDownloadResult(ok=False, error=str(exc))

    content = response.content
    digest = hashlib.sha256(content).hexdigest()
    expected = expected_sha256.strip().lower()
    if digest != expected:
        return FfmpegDownloadResult(
            ok=False,
            error=f"SHA-256 mismatch for {url}: expected {expected}, got {digest}",
        )

    return FfmpegDownloadResult(ok=True, content=content)


def download_manifest_entry(
    entry: ManifestEntry,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> FfmpegDownloadResult:
    """Download and verify the archive a manifest entry points at.

    Thin convenience wrapper over :func:`download_and_verify` so callers that
    already resolved a :class:`~collapsarr.ffmpeg_download.manifest.ManifestEntry`
    (via :func:`collapsarr.ffmpeg_download.manifest.get_manifest_entry`) don't
    need to unpack ``url``/``sha256`` themselves.
    """
    return download_and_verify(entry.url, entry.sha256, timeout=timeout, transport=transport)


__all__ = [
    "FfmpegDownloadResult",
    "download_and_verify",
    "download_manifest_entry",
]
