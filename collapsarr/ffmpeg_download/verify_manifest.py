"""Release-blocking check: every manifest entry resolves and checksum-verifies (COL-217).

Run as ``python -m collapsarr.ffmpeg_download.verify_manifest`` from
``release.yml``'s ``verify-ffmpeg-manifest`` job -- deliberately **not** part
of the `pytest` suite, since it makes real network calls against BtbN/
FFmpeg-Builds and evermeet.cx (the unit tests in
``tests/test_ffmpeg_download_client.py`` cover the same download+verify
logic entirely via ``httpx.MockTransport``, with no network access).

Exits non-zero -- failing the CI job, and so the release -- if any of the 5
target ``(platform, arch)`` pairs is missing from the manifest, or if its
pinned URL fails to resolve or its checksum no longer matches (a rotated/
removed upstream asset, or a stale pin), per ADR 0002's "CI verifying the pin
resolves for every target platform/arch before shipping" requirement.
"""

from __future__ import annotations

import sys

from .client import download_manifest_entry
from .manifest import TARGET_PLATFORM_ARCH_PAIRS, load_manifest

# BtbN builds run 100-170MB; evermeet's ~25MB. This check downloads all 5,
# so it needs meaningfully more headroom than the small JSON/API calls the
# rest of this package's clients make.
_VERIFY_TIMEOUT = 120.0


def verify_manifest() -> list[str]:
    """Verify every target (platform, arch) pair resolves and checksum-verifies.

    Returns a list of human-readable failure messages (empty = all green).
    Makes real, un-mocked ``httpx`` calls -- deliberately not exercised by
    the unit test suite (see this module's docstring).
    """
    manifest = load_manifest()
    failures: list[str] = []

    for platform, arch in TARGET_PLATFORM_ARCH_PAIRS:
        entry = manifest.get((platform, arch))
        if entry is None:
            failures.append(f"{platform}/{arch}: missing from manifest.json")
            continue

        result = download_manifest_entry(entry, timeout=_VERIFY_TIMEOUT)
        if not result.ok:
            failures.append(f"{platform}/{arch} ({entry.url}): {result.error}")
        else:
            print(f"OK: {platform}/{arch} v{entry.version} -- {entry.url}")

    return failures


def main() -> int:
    failures = verify_manifest()
    if failures:
        print("FFmpeg manifest verification failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("All FFmpeg manifest entries resolved and checksum-verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
