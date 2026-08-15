"""Tests for the FFmpeg download manifest (COL-217).

Loads the real, checked-in ``manifest.json`` (no network involved -- this
is a local file read) and asserts its shape: every one of the 5 target
``(platform, arch)`` pairs is present, and each entry has a well-formed
``version``/``url``/``sha256``.
"""

from __future__ import annotations

import re

from collapsarr.ffmpeg_download.manifest import (
    TARGET_PLATFORM_ARCH_PAIRS,
    ManifestEntry,
    get_manifest_entry,
    load_manifest,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def test_target_platform_arch_pairs_has_exactly_five_entries() -> None:
    assert len(TARGET_PLATFORM_ARCH_PAIRS) == 5
    # No duplicates.
    assert len(set(TARGET_PLATFORM_ARCH_PAIRS)) == 5


def test_manifest_covers_every_target_platform_arch_pair() -> None:
    manifest = load_manifest()

    for platform, arch in TARGET_PLATFORM_ARCH_PAIRS:
        assert (platform, arch) in manifest, f"manifest.json is missing {platform}/{arch}"


def test_every_manifest_entry_is_well_formed() -> None:
    manifest = load_manifest()

    for (platform, arch), entry in manifest.items():
        assert isinstance(entry, ManifestEntry)
        assert entry.platform == platform
        assert entry.arch == arch
        assert entry.version, f"{platform}/{arch}: empty version"
        assert entry.url.startswith("https://"), f"{platform}/{arch}: url must be HTTPS"
        sha_ok = _SHA256_RE.match(entry.sha256)
        assert sha_ok, f"{platform}/{arch}: sha256 is not 64 lowercase hex chars"


def test_get_manifest_entry_returns_a_matching_entry() -> None:
    entry = get_manifest_entry("linux", "amd64")

    assert entry is not None
    assert entry.platform == "linux"
    assert entry.arch == "amd64"


def test_get_manifest_entry_returns_none_for_an_unknown_pair() -> None:
    assert get_manifest_entry("solaris", "sparc") is None


def test_linux_and_windows_entries_are_hosted_by_btbn_ffmpeg_builds() -> None:
    manifest = load_manifest()

    for platform in ("linux", "windows"):
        for arch in ("amd64", "arm64"):
            entry = manifest.get((platform, arch))
            if entry is None:
                continue
            assert "github.com/BtbN/FFmpeg-Builds/releases/download/" in entry.url


def test_macos_entries_are_hosted_by_evermeet() -> None:
    manifest = load_manifest()

    for arch in ("amd64", "arm64"):
        entry = manifest[("macos", arch)]
        assert entry.url.startswith("https://evermeet.cx/")
