"""HTTP client for fetching and parsing the ``SHA256SUMS`` release asset (COL-230).

Mirrors :mod:`collapsarr.update_check.client`/:mod:`collapsarr.ffmpeg_download.
client`'s conventions exactly: an ``httpx.Client`` built with an injectable
``transport: httpx.BaseTransport | None = None`` (tests pass an
``httpx.MockTransport`` instead of making a real network call), and a
never-raising, ``ok``-flagged result dataclass -- connection errors, timeouts,
non-2xx responses, and a missing checksum entry are all captured as a failed
:class:`SelfUpdateChecksumResult` with a short human-readable ``error``,
rather than propagating an exception.

**File format.** ``SHA256SUMS`` is published by ``release.yml``'s
``sha256sum -- * > SHA256SUMS`` step (COL-227) -- standard ``sha256sum``
output, one entry per line: a 64-hex-character digest, then whitespace, then
the filename (optionally prefixed with ``*`` -- ``sha256sum``'s binary-mode
marker; the Linux runner that generates this file always runs in text mode,
i.e. no ``*``, but a ``*``-prefixed line still parses here so a checksum file
generated in binary mode elsewhere would too). :func:`parse_sha256sums` is
the pure, no-network parsing half; :func:`resolve_platform_arch` +
:func:`resolve_asset_filename` resolve which entry this process needs;
:func:`fetch_checksum_entry` combines fetch + parse + resolve into the one
call sites actually use.

**Filename convention** (``release.yml``'s ``native-build-*`` jobs, COL-227):
``collapsarr-<version>-{linux,macos}-{amd64,arm64}.tar.gz`` for Linux/macOS,
``collapsarr-<version>-windows-amd64.zip`` for Windows -- there is no
Windows/arm64 build.
"""

from __future__ import annotations

import platform
import re
from dataclasses import dataclass

import httpx

_DEFAULT_TIMEOUT = 10.0
_ERROR_BODY_LIMIT = 500

#: Matches one ``sha256sum``-style line: a 64-hex-char digest, one or more
#: whitespace characters, an optional ``*`` (binary-mode marker), then the
#: filename (the remainder of the line). Applied to an already-``.strip()``ed
#: line.
_SHA256SUMS_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")


@dataclass(frozen=True, slots=True)
class SelfUpdateChecksumResult:
    """Outcome of fetching + parsing ``SHA256SUMS`` and resolving one platform's entry.

    ``ok=False`` is the "no result" signal -- network error, timeout, non-2xx,
    or no matching filename entry in an otherwise-well-formed file -- with
    ``error`` carrying a short human-readable reason for logging. On success,
    ``filename``/``sha256`` are the resolved release-asset filename and its
    lowercase hex digest, matching :class:`~collapsarr.ffmpeg_download.
    manifest.ManifestEntry`'s ``sha256`` shape so a later ticket can feed it
    straight into the same :func:`~collapsarr.ffmpeg_download.client.
    download_and_verify` verified-download primitive.
    """

    ok: bool
    filename: str | None = None
    sha256: str | None = None
    error: str | None = None


def resolve_platform_arch() -> tuple[str, str] | None:
    """Return this process's ``(platform, arch)`` in release-asset key form, or ``None``.

    Maps :func:`platform.system`/:func:`platform.machine` onto the exact
    ``"linux"``/``"macos"``/``"windows"`` x ``"amd64"``/``"arm64"`` keys
    ``release.yml``'s native archive filenames use (COL-227) -- the same
    mapping :func:`collapsarr.ffmpeg_download.service.resolve_platform_arch`
    performs for the FFmpeg manifest's identically-shaped ``(platform, arch)``
    keys, reimplemented here (rather than imported) to keep
    :mod:`collapsarr.self_update` decoupled from :mod:`collapsarr.
    ffmpeg_download` -- unrelated concerns (the app's own binary vs. a
    third-party FFmpeg build) that happen to share a platform-detection
    shape. ``None`` for any platform/arch combination outside the 5
    ``release.yml`` builds (e.g. a 32-bit host, or an OS Collapsarr doesn't
    ship a native build for), so the caller can report "unsupported" rather
    than resolving a filename that could never exist.
    """
    system = platform.system().lower()
    if system == "linux":
        platform_name = "linux"
    elif system == "darwin":
        platform_name = "macos"
    elif system == "windows":
        platform_name = "windows"
    else:
        return None

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        return None

    return platform_name, arch


def resolve_asset_filename(version: str, platform_name: str, arch: str) -> str:
    """Build the expected release-asset filename for ``(version, platform_name, arch)``.

    Matches ``release.yml``'s (COL-227) native archive naming exactly: a
    ``"windows"`` entry is a ``.zip``, every other platform a ``.tar.gz``.
    ``version`` is expected without a leading ``v`` (matches ``release.yml``'s
    ``$VERSION``, the tag with its ``v`` prefix stripped).
    """
    if platform_name == "windows":
        return f"collapsarr-{version}-windows-{arch}.zip"
    return f"collapsarr-{version}-{platform_name}-{arch}.tar.gz"


def parse_sha256sums(content: str) -> dict[str, str]:
    """Parse a ``SHA256SUMS`` file's contents into ``{filename: lowercase hex sha256}``.

    Never raises: blank lines and any line that doesn't match the expected
    ``<64-hex-char digest><whitespace>[*]<filename>`` shape are silently
    skipped rather than raising -- a checksum file this parser can't fully
    make sense of should not crash the caller; an unresolvable platform/arch
    entry is instead surfaced through :func:`resolve_checksum_entry`'s own
    ``ok=False`` result.
    """
    entries: dict[str, str] = {}
    for raw_line in content.splitlines():
        match = _SHA256SUMS_LINE.match(raw_line.strip())
        if match is None:
            continue
        digest, filename = match.groups()
        entries[filename.strip()] = digest.lower()
    return entries


def resolve_checksum_entry(
    content: str, version: str, platform_name: str, arch: str
) -> SelfUpdateChecksumResult:
    """Parse ``content`` and resolve the entry for ``(version, platform_name, arch)``.

    ``ok=False`` when the parsed ``SHA256SUMS`` content has no entry for the
    expected filename -- e.g. a release cut before COL-227 (no
    ``SHA256SUMS`` asset at all, so ``content`` parses to an empty mapping),
    or (shouldn't happen for any release cut by ``release.yml`` since
    COL-227, but defensive nonetheless) a checksums file missing this
    specific platform/arch's entry.
    """
    filename = resolve_asset_filename(version, platform_name, arch)
    entries = parse_sha256sums(content)
    sha256 = entries.get(filename)
    if sha256 is None:
        return SelfUpdateChecksumResult(
            ok=False, error=f"No checksum entry for {filename!r} in SHA256SUMS"
        )
    return SelfUpdateChecksumResult(ok=True, filename=filename, sha256=sha256)


def fetch_sha256sums(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str | None, str | None]:
    """Fetch the raw ``SHA256SUMS`` text at ``url``. Never raises.

    Returns ``(content, None)`` on success or ``(None, error)`` on failure --
    connection error, timeout, or a non-2xx response -- mirroring
    :func:`collapsarr.update_check.client.fetch_latest_release`'s
    never-raising contract at the transport level. Kept separate from
    :func:`fetch_checksum_entry` so a caller that already has the raw text
    (e.g. a test fixture, or a future caller that fetched it for another
    purpose) can call :func:`resolve_checksum_entry` directly without a
    network round-trip.
    """
    client = (
        httpx.Client(timeout=timeout, transport=transport)
        if transport is not None
        else httpx.Client(timeout=timeout)
    )
    try:
        with client:
            response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code}: {exc.response.text}"[:_ERROR_BODY_LIMIT]
        return None, detail
    except httpx.HTTPError as exc:
        return None, str(exc)
    return response.text, None


def fetch_checksum_entry(
    url: str,
    version: str,
    platform_name: str,
    arch: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> SelfUpdateChecksumResult:
    """Fetch, parse, and resolve the checksum entry for this platform/arch/version.

    The one seam callers (COL-232+'s apply flow) actually use: combines
    :func:`fetch_sha256sums` + :func:`resolve_checksum_entry`, never raising,
    matching :func:`collapsarr.update_check.client.fetch_latest_release`'s
    contract end to end.
    """
    content, error = fetch_sha256sums(url, timeout=timeout, transport=transport)
    if content is None:
        return SelfUpdateChecksumResult(ok=False, error=error)
    return resolve_checksum_entry(content, version, platform_name, arch)


__all__ = [
    "SelfUpdateChecksumResult",
    "fetch_checksum_entry",
    "fetch_sha256sums",
    "parse_sha256sums",
    "resolve_asset_filename",
    "resolve_checksum_entry",
    "resolve_platform_arch",
]
