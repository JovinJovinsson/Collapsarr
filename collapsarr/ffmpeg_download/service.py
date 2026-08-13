"""Orchestrates a full FFmpeg auto-download (COL-222, ADR 0002).

The service-layer seam :mod:`collapsarr.ffmpeg_download.routes`' ``POST
/api/system/ffmpeg/download`` calls: resolve the running process's
``(platform, arch)``, look up the pinned manifest entry (COL-217's
:mod:`~collapsarr.ffmpeg_download.manifest`), download + SHA-256-verify it
(COL-217's :mod:`~collapsarr.ffmpeg_download.client`), extract just the
``ffmpeg`` executable into ``<data_dir>/ffmpeg/``, and persist the resolved
absolute path onto ``GlobalSettings.ffmpeg_path`` (COL-218's
:func:`collapsarr.settings.service.set_ffmpeg_path`).

Never raises on an expected failure mode (unsupported platform, no pinned
manifest entry, network/checksum failure, a malformed or ffmpeg-less
archive) -- every one of those is returned as a failed
:class:`FfmpegDownloadOutcome` with a short, human-readable ``error``, the
same never-raising contract :mod:`collapsarr.ffmpeg_download.client`
already follows. ``GlobalSettings.ffmpeg_path`` is written **only** once
every prior step has succeeded -- a failed/partial download never leaves a
half-written or unverified path behind, satisfying this ticket's "no
silent fallback to an unverified binary" requirement by construction: there
is exactly one write to ``ffmpeg_path`` in this whole module, and it is the
very last statement on the success path.

Archive handling, and why this module does *not* reuse
:mod:`collapsarr.restore.upload`'s decompression-bomb ceiling:

* **Zip-slip / tar-slip rejection** -- every entry in the archive (zip or
  tar) is checked against the shared
  :func:`collapsarr.archive_safety.is_unsafe_archive_entry_path` guard before
  anything is read -- the same check :mod:`collapsarr.restore.upload` uses
  for its own untrusted-upload extraction, extracted into its own module
  (COL-222) precisely so this security-sensitive check has exactly one
  implementation rather than two that could silently drift apart. The
  archive is rejected outright if *any* entry is unsafe, rather than merely
  skipped -- same "refuse a hostile archive, don't just ignore its bad
  parts" posture as that module.
* Only a **single named member** (``ffmpeg``/``ffmpeg.exe``, matched by
  basename) is ever read out of the archive, and it is written to a path
  *this module constructs* (``<data_dir>/ffmpeg/<name>``) -- never to a path
  derived from the archive's own entry name. So even without the zip-slip
  check above, nothing this module writes could ever land outside the
  extraction root; the check is deliberately-redundant defence-in-depth, not
  the only thing standing between an archive and the filesystem.
* For tar archives, only :meth:`tarfile.TarInfo.isfile` members are ever
  candidates -- a symlink/hardlink/device entry named ``ffmpeg`` is never
  followed or treated as the binary.
* **No decompression-bomb ceiling** (unlike
  :mod:`collapsarr.restore.upload`'s ``MAX_UNCOMPRESSED_BYTES``): that
  module's bytes are genuinely untrusted -- arbitrary content a user
  uploads, with no prior integrity check. This module's archive bytes, by
  contrast, are SHA-256-verified against a checksum Collapsarr itself pinned
  in ``manifest.json`` *before* extraction ever runs (see
  :func:`~collapsarr.ffmpeg_download.client.download_and_verify`) -- so by
  the time extraction starts, the bytes are already proven byte-for-byte
  identical to a specific, previously-vetted upstream release artifact
  (BtbN/FFmpeg-Builds or evermeet.cx). The only way a bomb-like member could
  appear here is if the pin in ``manifest.json`` itself were compromised --
  a supply-chain risk at the "who edits the manifest" boundary, not one an
  extraction-time byte ceiling would catch (the ceiling would just reject
  the pinned-and-trusted archive too). The compressed *download* itself is
  still bounded -- see :data:`MAX_FFMPEG_ARCHIVE_BYTES` and
  :func:`~collapsarr.ffmpeg_download.client.download_and_verify`'s
  ``max_bytes``.
"""

from __future__ import annotations

import os
import platform
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from ..archive_safety import is_unsafe_archive_entry_path
from ..settings.service import set_ffmpeg_path
from .client import download_manifest_entry
from .manifest import get_manifest_entry

#: Subdirectory of ``<data_dir>`` the extracted ``ffmpeg`` binary is written
#: to, per this ticket's "extract into ``<data_dir>/ffmpeg/``" requirement.
FFMPEG_SUBDIR = "ffmpeg"

#: Generous timeout for the full archive download -- BtbN builds run
#: 100-170MB (see :mod:`collapsarr.ffmpeg_download.verify_manifest`), well
#: beyond the small JSON/API calls the rest of this package's clients make.
DOWNLOAD_TIMEOUT = 180.0

#: Hard ceiling on the *downloaded* archive size (COL-222 design note, see
#: module docstring) -- generous headroom above the largest known real
#: manifest entry (~170MB BtbN builds) while still bounding this process's
#: peak memory during a user-triggered download against a compromised/
#: misbehaving redirect target. Forwarded to
#: :func:`~collapsarr.ffmpeg_download.client.download_and_verify` as
#: ``max_bytes``.
MAX_FFMPEG_ARCHIVE_BYTES = 400 * 1024 * 1024

#: Basenames this module will extract from an archive, matched
#: case-insensitively. ``ffmpeg.exe`` for the Windows manifest entry;
#: ``ffmpeg`` for every other platform.
_FFMPEG_BINARY_BASENAMES = frozenset({"ffmpeg", "ffmpeg.exe"})


class FfmpegExtractionError(RuntimeError):
    """The downloaded, checksum-verified archive couldn't be safely extracted.

    Raised for a malformed archive, an archive containing an unsafe
    (zip-slip/tar-slip) entry path, or an archive with no recognisable
    ``ffmpeg``/``ffmpeg.exe`` member. Always caught within this module --
    :func:`download_and_install_ffmpeg` turns it into a failed
    :class:`FfmpegDownloadOutcome`, never lets it propagate.
    """


@dataclass(frozen=True, slots=True)
class FfmpegDownloadOutcome:
    """Outcome of a full download-verify-extract-persist attempt.

    ``ok=False`` covers every failure mode this module can hit: an
    unsupported platform/arch, no pinned manifest entry, a network/checksum
    failure (COL-217's :class:`~collapsarr.ffmpeg_download.client.
    FfmpegDownloadResult`), or an extraction failure -- with ``error``
    carrying a short, human-readable reason the route layer passes straight
    through to the caller. ``GlobalSettings.ffmpeg_path`` is left completely
    untouched on every ``ok=False`` outcome; see the module docstring.
    """

    ok: bool
    ffmpeg_path: str | None = None
    error: str | None = None


def resolve_platform_arch() -> tuple[str, str] | None:
    """Return this process's ``(platform, arch)`` in manifest-key form, or ``None``.

    Maps :func:`platform.system`/:func:`platform.machine` onto the exact
    ``"linux"``/``"macos"``/``"windows"`` x ``"amd64"``/``"arm64"`` keys
    :mod:`collapsarr.ffmpeg_download.manifest` uses (mirroring
    ``release.yml``'s ``native-build-*`` matrix naming) -- ``None`` for any
    platform/arch combination outside the 5 the manifest covers (e.g. a
    32-bit host, or an OS Collapsarr doesn't ship a native build for), so the
    caller can report "unsupported" rather than looking up a key that could
    never exist.
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


def _extract_from_zip(content: bytes) -> tuple[str, bytes]:
    """Extract the ``ffmpeg``/``ffmpeg.exe`` member from a zip archive's bytes.

    Returns ``(basename, data)``. Raises :class:`FfmpegExtractionError` for a
    corrupt zip, an unsafe entry path, or no matching member. Picks the
    shallowest matching entry (fewest path separators) if more than one
    matches, e.g. a Windows build nesting the binary under ``bin/``.
    """
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            for info in archive.infolist():
                if is_unsafe_archive_entry_path(info.filename):
                    raise FfmpegExtractionError(
                        "The downloaded archive contains an unsafe entry path "
                        f"({info.filename!r}); it looks like a path-traversal "
                        "(zip-slip) attempt and was rejected."
                    )
            candidates = [
                info
                for info in archive.infolist()
                if not info.is_dir()
                and Path(info.filename).name.lower() in _FFMPEG_BINARY_BASENAMES
            ]
            if not candidates:
                raise FfmpegExtractionError(
                    "The downloaded archive does not contain an ffmpeg executable."
                )
            member = min(candidates, key=lambda info: info.filename.count("/"))
            return Path(member.filename).name.lower(), archive.read(member)
    except zipfile.BadZipFile as exc:
        raise FfmpegExtractionError("The downloaded archive is not a valid zip file.") from exc


def _extract_from_tar(content: bytes) -> tuple[str, bytes]:
    """Extract the ``ffmpeg`` member from a tar (``.tar.xz``/``.tar.gz``/etc) archive's bytes.

    Returns ``(basename, data)``. Raises :class:`FfmpegExtractionError` for a
    corrupt archive, an unsafe entry path, or no matching *regular file*
    member -- a symlink/hardlink/device entry named ``ffmpeg`` never counts
    as a candidate (:meth:`tarfile.TarInfo.isfile`), so it can never be
    followed. ``mode="r:*"`` auto-detects the compression (BtbN's Linux
    builds ship ``.tar.xz``).
    """
    try:
        with tarfile.open(fileobj=BytesIO(content), mode="r:*") as archive:
            members = archive.getmembers()
            for member in members:
                if is_unsafe_archive_entry_path(member.name):
                    raise FfmpegExtractionError(
                        "The downloaded archive contains an unsafe entry path "
                        f"({member.name!r}); it looks like a path-traversal "
                        "(tar-slip) attempt and was rejected."
                    )
            candidates = [
                member
                for member in members
                if member.isfile() and Path(member.name).name.lower() in _FFMPEG_BINARY_BASENAMES
            ]
            if not candidates:
                raise FfmpegExtractionError(
                    "The downloaded archive does not contain an ffmpeg executable."
                )
            member = min(candidates, key=lambda m: m.name.count("/"))
            extracted = archive.extractfile(member)
            if extracted is None:  # pragma: no cover - isfile() already excludes this
                raise FfmpegExtractionError(
                    "The downloaded archive's ffmpeg entry could not be read."
                )
            return Path(member.name).name.lower(), extracted.read()
    except tarfile.TarError as exc:
        raise FfmpegExtractionError("The downloaded archive is not a valid tar archive.") from exc


def _extract_ffmpeg_binary(content: bytes, source_url: str) -> tuple[str, bytes]:
    """Dispatch to the zip or tar extractor based on ``source_url``'s suffix.

    Every current manifest entry is either ``.zip`` (Windows, macOS) or
    ``.tar.xz`` (Linux) -- see ``manifest.json`` -- so the URL's own
    extension is a reliable, already-available signal; no content-sniffing
    needed.
    """
    if source_url.lower().endswith(".zip"):
        return _extract_from_zip(content)
    return _extract_from_tar(content)


def _write_ffmpeg_binary(data: bytes, dest_dir: Path, *, name: str) -> Path:
    """Atomically write the extracted binary to ``dest_dir/name``.

    Streamed through a same-directory dot-prefixed temp file and
    :func:`os.replace`-d into place -- mirrors
    :mod:`collapsarr.backup.service`'s atomic-rename convention, so a reader
    (or a crash mid-write) never observes a partial binary at the final path.
    Marked executable on POSIX (a no-op, harmless call on Windows, where
    ``.exe`` extension alone governs executability).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    final_path = dest_dir / name
    tmp_path = dest_dir / f".{name}.part"
    tmp_path.write_bytes(data)
    if os.name == "posix":
        current_mode = tmp_path.stat().st_mode
        tmp_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp_path, final_path)
    return final_path


def download_and_install_ffmpeg(
    session: Session,
    *,
    data_dir: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DOWNLOAD_TIMEOUT,
) -> FfmpegDownloadOutcome:
    """Download, verify, extract, and persist FFmpeg for this install (COL-222).

    The full orchestration ``POST /api/system/ffmpeg/download``
    (:mod:`collapsarr.ffmpeg_download.routes`) delegates to:

    1. Resolve ``(platform, arch)`` (:func:`resolve_platform_arch`) and look
       up its pinned manifest entry (:func:`~collapsarr.ffmpeg_download.
       manifest.get_manifest_entry`) -- ``ok=False`` if either is unavailable.
    2. Download + SHA-256-verify the archive
       (:func:`~collapsarr.ffmpeg_download.client.download_manifest_entry`,
       bounded by :data:`MAX_FFMPEG_ARCHIVE_BYTES`) -- ``ok=False`` on any
       network/checksum failure, with that failure's own ``error`` passed
       straight through.
    3. Extract just the ``ffmpeg``/``ffmpeg.exe`` member into
       ``<data_dir>/ffmpeg/`` (:func:`_extract_ffmpeg_binary` +
       :func:`_write_ffmpeg_binary`) -- ``ok=False`` on a malformed or
       ffmpeg-less archive.
    4. Persist the resolved absolute path onto ``GlobalSettings.ffmpeg_path``
       (:func:`collapsarr.settings.service.set_ffmpeg_path`) -- the *only*
       write in this whole flow, reached only once every step above has
       succeeded. The FFmpeg presence health check
       (:func:`collapsarr.health.ffmpeg.make_ffmpeg_check_run`) already reads
       this column live on every tick, so the ``ffmpeg_missing`` check
       reports available on the very next tick -- no restart required.

    ``transport``/``timeout`` are forwarded to the download step, letting
    tests inject an ``httpx.MockTransport`` (mirrors every other
    network-touching seam in this codebase).
    """
    resolved = resolve_platform_arch()
    if resolved is None:
        return FfmpegDownloadOutcome(
            ok=False,
            error=(
                f"FFmpeg auto-download is not available for this platform "
                f"({platform.system()}/{platform.machine()})."
            ),
        )
    platform_name, arch = resolved

    entry = get_manifest_entry(platform_name, arch)
    if entry is None:
        return FfmpegDownloadOutcome(
            ok=False,
            error=f"No pinned FFmpeg build is available for {platform_name}/{arch}.",
        )

    download_result = download_manifest_entry(
        entry, timeout=timeout, transport=transport, max_bytes=MAX_FFMPEG_ARCHIVE_BYTES
    )
    if not download_result.ok or download_result.content is None:
        return FfmpegDownloadOutcome(ok=False, error=download_result.error)

    dest_dir = Path(data_dir).expanduser() / FFMPEG_SUBDIR
    try:
        binary_name, binary_bytes = _extract_ffmpeg_binary(download_result.content, entry.url)
        final_path = _write_ffmpeg_binary(binary_bytes, dest_dir, name=binary_name)
    except FfmpegExtractionError as exc:
        return FfmpegDownloadOutcome(ok=False, error=str(exc))

    resolved_path = str(final_path.resolve())
    set_ffmpeg_path(session, resolved_path)
    return FfmpegDownloadOutcome(ok=True, ffmpeg_path=resolved_path)


__all__ = [
    "DOWNLOAD_TIMEOUT",
    "FFMPEG_SUBDIR",
    "MAX_FFMPEG_ARCHIVE_BYTES",
    "FfmpegDownloadOutcome",
    "FfmpegExtractionError",
    "download_and_install_ffmpeg",
    "resolve_platform_arch",
]
