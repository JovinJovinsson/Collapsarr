"""Pure version-identity comparison for the stable and beta channels (COL-86, COL-88, COL-96).

Deliberately **not** semver ordering: the running instance's version either
*is* the latest tag/SHA or it *isn't*. This module only ever answers "does
the running version match the latest known release", with no I/O and no
knowledge of :mod:`collapsarr.update_check.client` or the database.

The stable channel (:func:`is_up_to_date`/:func:`running_version_tag`) is a
straight identity comparison of the whole tag string: ``v<version>`` against a
fetched release's ``tag_name``. The beta channel
(:func:`is_up_to_date_beta`, COL-96) compares a *structured* ``(base, build)``
identity instead -- the beta workflow (``.github/workflows/beta.yml``) stamps
the running ``__version__`` with a fully-orderable ``<base>.<build>+beta``
release+local segment (e.g. ``0.2.1.0007+beta``) and tags each beta Release
``beta-v<base>.<build>`` (e.g. ``beta-v0.2.1.0007``) using the *same* base+build
identity. It cannot compare those as raw strings, though: an installed wheel's
``__version__`` has been PEP 440-normalized (leading zeros stripped from the
release segment, so ``0.2.1.0007+beta`` reports as ``0.2.1.7+beta``) while the
Release *tag* stays zero-padded (it's a plain string, never parsed as a
version). So it parses both sides into ``(base, build)`` and compares the build
as an ``int`` (:func:`_beta_identity`), immune to that padding difference. The
old scheme (COL-88) embedded a git short-SHA and needed to be un-embedded on
both sides before comparing; the build number carries the identity now, so that
extraction is gone.
"""

from __future__ import annotations

import re

from collapsarr.settings.models import BETA_LOCAL_SEGMENT_PREFIX

VERSION_TAG_PREFIX = "v"
"""Tags this repo's release workflow cuts are prefixed with this (``v1.2.3``),
matching the git tag pattern documented in ``docs/TRACKER.md`` /
``CONTEXT.md``'s Release Channel entry (``v*.*.*`` on ``main``)."""

BETA_TAG_PREFIX = "beta-v"
"""The beta channel's GitHub Release tag prefix: the workflow tags each beta
Release ``beta-v<base>.<build>`` (see ``.github/workflows/beta.yml``'s
``github-release`` job), i.e. the stable ``v``-prefix with a ``beta-`` marker
in front, wrapping the same ``<base>.<build>`` identity carried by the running
version's public part."""


def running_version_tag(version: str) -> str:
    """Return the tag identity of a running ``__version__`` string.

    ``f"v{version}"`` -- the exact form :func:`is_up_to_date` compares against
    a fetched release's ``tag_name``. A tiny named helper so both call sites
    (this module's comparison and any future caller needing the same
    identity) can't drift on the ``f"v{...}"`` formatting.
    """
    return f"{VERSION_TAG_PREFIX}{version}"


def is_up_to_date(current_version: str, latest_tag: str | None) -> bool:
    """Return whether ``current_version`` matches ``latest_tag`` by identity.

    ``current_version`` is the running ``collapsarr.__version__`` (no ``v``
    prefix); ``latest_tag`` is a fetched release's ``tag_name`` (e.g. from
    :class:`~collapsarr.update_check.client.GitHubReleaseResult.tag`), already
    ``v``-prefixed as GitHub tags it. A ``None`` ``latest_tag`` (no
    successful fetch yet) is reported as *not* up to date -- "unknown" is
    treated the same as "an update might be available" rather than silently
    claiming the instance is current with no evidence.
    """
    if latest_tag is None:
        return False
    return running_version_tag(current_version) == latest_tag


def public_version(version: str) -> str:
    """Strip a PEP 440 local version segment (the part after ``+``).

    E.g. ``public_version("0.2.1.0007+beta") == "0.2.1.0007"``. A version with
    no local segment is returned unchanged. Used by
    :func:`running_beta_version_tag` to recover the ``<base>.<build>`` identity
    that the beta Release tag (``beta-v<base>.<build>``) is cut from.
    """
    return version.split("+", 1)[0]


def running_beta_version_tag(version: str) -> str:
    """Return the beta Release tag string a running beta ``__version__`` maps to.

    ``f"beta-v{public_version(version)}"`` -- a display/reconstruction helper
    (beta counterpart of :func:`running_version_tag`), dropping the ``+beta``
    local segment first. **Not** the basis for identity comparison: a running
    version's release segment has been PEP 440-normalized (leading zeros
    stripped -- ``0.2.1.0007+beta`` installs as ``0.2.1.7+beta``), so this
    yields ``beta-v0.2.1.7`` where the freshly-cut CI tag is the zero-padded
    ``beta-v0.2.1.0007``. :func:`is_up_to_date_beta` therefore compares
    *structured* ``(base, build)`` identities via :func:`_beta_identity`, not
    these strings, so the padding difference doesn't matter (COL-96).
    """
    return f"{BETA_TAG_PREFIX}{public_version(version)}"


_BETA_IDENTITY_RE = re.compile(r"^(\d+\.\d+\.\d+)\.(\d+)$")
"""Splits a beta public version ``<major>.<minor>.<patch>.<build>`` into its
``(base, build)`` groups. Applied to both the running version's public part and
the fetched tag's ``beta-v`` suffix so the build number can be compared as an
``int`` -- immune to the leading-zero difference between a normalized installed
version (``0.2.1.7``) and a zero-padded CI-cut tag (``0.2.1.0007``)."""


def _beta_identity(public: str) -> tuple[str, int] | None:
    """Parse a beta public version into a zero-padding-immune ``(base, build)``.

    ``"0.2.1.0007"`` and the PEP 440-normalized ``"0.2.1.7"`` both parse to
    ``("0.2.1", 7)`` -- the build number is compared as an ``int``, so the
    leading zeros a CI-cut tag keeps but an installed wheel's metadata loses no
    longer break the match. Returns ``None`` for anything that isn't the
    expected ``<base>.<build>`` shape (a bare ``<base>``, a stable ``v`` tag's
    suffix, junk), which the caller treats as "no beta identity -> not a match".
    """
    match = _BETA_IDENTITY_RE.match(public)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def is_up_to_date_beta(current_version: str, latest_tag: str | None) -> bool:
    """Return whether the running beta version matches the latest prerelease tag.

    Beta-channel counterpart of :func:`is_up_to_date`: still pure identity
    comparison (no semver *ordering*), but comparing the structured
    ``(base, build)`` identity (:func:`_beta_identity`) of the running version's
    public part against that of the fetched ``beta-v<base>.<build>`` tag's
    suffix, rather than matching the two tag *strings* verbatim. This is
    deliberate (COL-96): the running ``__version__`` an installed wheel reports
    has been PEP 440-normalized -- ``0.2.1.0007+beta`` becomes ``0.2.1.7+beta``
    -- while the GitHub Release tag stays the zero-padded ``beta-v0.2.1.0007``
    (a plain string, never parsed as a version), so an exact string match would
    spuriously fail for every build past ``0009``. Comparing ``int`` build
    numbers makes the check immune to that padding difference.

    A ``None`` ``latest_tag`` (no successful fetch yet) reports "not up to
    date", same as :func:`is_up_to_date`. A **stable** running version (no
    :data:`~collapsarr.settings.models.BETA_LOCAL_SEGMENT_PREFIX` marker) also
    reports "not up to date" when checked against the beta channel -- there is
    no beta identity to compare, so this is the documented "switch to beta,
    see 'update available' with nothing to compare yet" edge case (COL-88's
    acceptance criteria), not a bug. A non-beta tag (no ``beta-v`` prefix) or
    either side failing to parse as ``<base>.<build>`` is likewise "not a
    match".
    """
    if latest_tag is None:
        return False
    if BETA_LOCAL_SEGMENT_PREFIX not in current_version:
        return False
    if not latest_tag.startswith(BETA_TAG_PREFIX):
        return False

    running_identity = _beta_identity(public_version(current_version))
    tag_identity = _beta_identity(latest_tag[len(BETA_TAG_PREFIX) :])
    if running_identity is None or tag_identity is None:
        return False
    return running_identity == tag_identity


__all__ = [
    "BETA_LOCAL_SEGMENT_PREFIX",
    "BETA_TAG_PREFIX",
    "VERSION_TAG_PREFIX",
    "is_up_to_date",
    "is_up_to_date_beta",
    "public_version",
    "running_beta_version_tag",
    "running_version_tag",
]
