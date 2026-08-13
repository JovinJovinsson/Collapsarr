"""FFmpeg version manifest, verified download client, and auto-download trigger
(COL-217, COL-222, ADR 0002).

A per-release manifest pinning one checksummed FFmpeg build per
``(platform, arch)`` pair, an httpx-based client that downloads and
SHA-256-verifies it, and the orchestration + ``POST`` endpoint that wire the
verified download into ``<data_dir>/ffmpeg/`` and
``GlobalSettings.ffmpeg_path``.

Public surface:

- **Manifest** -- :func:`get_manifest_entry` / :func:`load_manifest`
  (:mod:`~collapsarr.ffmpeg_download.manifest`): read ``manifest.json``,
  keyed by ``(platform, arch)``. :data:`TARGET_PLATFORM_ARCH_PAIRS` lists the
  5 combinations every entry must cover (mirrors ``release.yml``'s
  ``native-build-*`` matrix).
- **Client** -- :func:`download_and_verify` / :func:`download_manifest_entry`
  + :class:`FfmpegDownloadResult` (:mod:`~collapsarr.ffmpeg_download.client`):
  an unauthenticated, never-raising HTTPS fetch + SHA-256 check, mirroring
  :mod:`collapsarr.update_check.client`'s ``fetch_latest_release`` pattern
  (injectable ``transport`` for tests).
- **Auto-download orchestration** -- :func:`download_and_install_ffmpeg` /
  :func:`resolve_platform_arch` + :class:`FfmpegDownloadOutcome`
  (:mod:`~collapsarr.ffmpeg_download.service`, COL-222): resolves this
  process's ``(platform, arch)``, downloads + verifies the pinned build,
  extracts just the ``ffmpeg`` executable into ``<data_dir>/ffmpeg/``, and
  persists the resolved path onto ``GlobalSettings.ffmpeg_path`` -- the whole
  opt-in trigger behind ``POST /api/system/ffmpeg/download``
  (:mod:`~collapsarr.ffmpeg_download.routes`).
- **Release-blocking verification** --
  :func:`~collapsarr.ffmpeg_download.verify_manifest.verify_manifest`
  (``python -m collapsarr.ffmpeg_download.verify_manifest``): a real-network
  check, run from ``release.yml``, that every manifest entry still resolves
  and checksum-verifies -- deliberately excluded from the (network-free)
  ``pytest`` suite.
"""

from __future__ import annotations

from .client import FfmpegDownloadResult, download_and_verify, download_manifest_entry
from .manifest import TARGET_PLATFORM_ARCH_PAIRS, ManifestEntry, get_manifest_entry, load_manifest
from .service import (
    FfmpegDownloadOutcome,
    FfmpegExtractionError,
    download_and_install_ffmpeg,
    resolve_platform_arch,
)

__all__ = [
    "TARGET_PLATFORM_ARCH_PAIRS",
    "FfmpegDownloadOutcome",
    "FfmpegDownloadResult",
    "FfmpegExtractionError",
    "ManifestEntry",
    "download_and_install_ffmpeg",
    "download_and_verify",
    "download_manifest_entry",
    "get_manifest_entry",
    "load_manifest",
    "resolve_platform_arch",
]
