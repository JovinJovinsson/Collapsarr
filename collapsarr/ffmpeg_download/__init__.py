"""FFmpeg version manifest + verified download client (COL-217, ADR 0002).

The foundational slice of "FFmpeg auto-download" (ADR 0002): a per-release
manifest pinning one checksummed FFmpeg build per ``(platform, arch)`` pair,
and an httpx-based client that downloads and SHA-256-verifies it. Deliberately
scoped to *fetching a verified binary* only -- wiring this into the opt-in
prompt / downmix pipeline's ``ffmpeg_path`` resolution (ADR 0002's "Wired into
the downmix pipeline via a new persisted ``ffmpeg_path`` field") is later,
follow-on work (COL-222), not this module's concern.

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

__all__ = [
    "TARGET_PLATFORM_ARCH_PAIRS",
    "FfmpegDownloadResult",
    "ManifestEntry",
    "download_and_verify",
    "download_manifest_entry",
    "get_manifest_entry",
    "load_manifest",
]
