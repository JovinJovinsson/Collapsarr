"""Per-release FFmpeg download manifest (COL-217).

``manifest.json`` (bundled as package data next to this module, same
"data file lives beside the code that reads it" convention as
:mod:`collapsarr.frontend`'s ``static/``) pins one FFmpeg build per
``(platform, arch)`` pair Collapsarr ships a native artifact for -- the same
five combinations ``release.yml``'s ``native-build-*`` matrix jobs cover
(``linux``/``amd64``+``arm64``, ``macos``/``amd64``+``arm64``,
``windows``/``amd64``). Per ADR 0002 ("FFmpeg auto-download is opt-in,
checksum-verified, and version-pinned"):

* **Linux + Windows** -- BtbN/FFmpeg-Builds (GitHub Releases), pinned to a
  dated ``autobuild-*`` tag rather than the continuously-rewritten ``latest``
  tag, so the pin is reproducible. Each entry uses the static ``gpl`` build
  (no shared-library dependency at runtime).
* **macOS** -- evermeet.cx, the only established, actively-maintained
  provider of prebuilt macOS FFmpeg binaries (BtbN does not publish macOS
  builds). evermeet.cx does not build natively for Apple Silicon (its own
  site: "I do not plan to provide native ffmpeg binaries for Apple Silicon
  ARM") -- the ``macos``/``arm64`` entry deliberately points at the same
  x86_64 build as ``macos``/``amd64``; it runs unmodified under Rosetta 2 on
  Apple Silicon Macs, which is the standard fallback for this exact gap.
* **No GPG dependency anywhere** -- every entry's ``sha256`` is a checksum
  Collapsarr computed itself from the downloaded bytes at pin time (not
  merely copied from a provider-published checksum file), matching ADR
  0002's "HTTPS + SHA-256 checksum only" decision.

:func:`get_manifest_entry` is the read path :mod:`collapsarr.ffmpeg_download.
client` and the release-blocking CI check
(:mod:`collapsarr.ffmpeg_download.verify_manifest`) both use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"

#: The 5 target (platform, arch) pairs every manifest entry must cover --
#: mirrors release.yml's native-build-linux/-macos/-windows matrix exactly.
TARGET_PLATFORM_ARCH_PAIRS: tuple[tuple[str, str], ...] = (
    ("linux", "amd64"),
    ("linux", "arm64"),
    ("macos", "amd64"),
    ("macos", "arm64"),
    ("windows", "amd64"),
)


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One manifest entry: the pinned FFmpeg build for a single ``(platform, arch)``."""

    platform: str
    arch: str
    version: str
    url: str
    sha256: str


def _load_raw() -> dict[str, object]:
    """Read and parse ``manifest.json`` from disk. Raises on a malformed file --
    a broken manifest is a packaging bug, not a runtime "no result" outcome.
    """
    raw: object = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"{_MANIFEST_PATH} must contain a JSON object at the top level"
        raise ValueError(msg)
    return raw


def _require_str(value: object, *, field: str, platform: str, arch: str) -> str:
    """Validate a manifest entry field is a non-empty string, or raise.

    ``json.loads`` returns ``Any``, so mypy strict alone won't catch a
    malformed pin (e.g. ``version`` accidentally written as a number) -- this
    mirrors :func:`collapsarr.restore.marker.read_restore_marker`'s
    ``isinstance`` guard on parsed JSON, so a bad edit to ``manifest.json``
    fails loudly here rather than silently producing a wrongly-typed
    :class:`ManifestEntry`.
    """
    if not isinstance(value, str) or not value:
        msg = (
            f"{platform}/{arch}: manifest field {field!r} "
            f"must be a non-empty string, got {value!r}"
        )
        raise ValueError(msg)
    return value


def load_manifest() -> dict[tuple[str, str], ManifestEntry]:
    """Load every entry in ``manifest.json``, keyed by ``(platform, arch)``."""
    raw = _load_raw()
    manifest: dict[tuple[str, str], ManifestEntry] = {}
    for platform_name, arches in raw.items():
        if not isinstance(arches, dict):
            msg = f"{platform_name}: manifest platform entry must be a JSON object"
            raise ValueError(msg)
        for arch_name, entry in arches.items():
            if not isinstance(entry, dict):
                msg = f"{platform_name}/{arch_name}: manifest entry must be a JSON object"
                raise ValueError(msg)
            manifest[(platform_name, arch_name)] = ManifestEntry(
                platform=platform_name,
                arch=arch_name,
                version=_require_str(
                    entry.get("version"), field="version", platform=platform_name, arch=arch_name
                ),
                url=_require_str(
                    entry.get("url"), field="url", platform=platform_name, arch=arch_name
                ),
                sha256=_require_str(
                    entry.get("sha256"), field="sha256", platform=platform_name, arch=arch_name
                ),
            )
    return manifest


def get_manifest_entry(platform: str, arch: str) -> ManifestEntry | None:
    """Look up the pinned build for ``(platform, arch)``, or ``None`` if absent."""
    return load_manifest().get((platform, arch))


__all__ = [
    "TARGET_PLATFORM_ARCH_PAIRS",
    "ManifestEntry",
    "get_manifest_entry",
    "load_manifest",
]
