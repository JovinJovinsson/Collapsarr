"""Pure version-identity comparison for the stable and beta channels (COL-86, COL-88, COL-96).

Deliberately **not** semver ordering: the running instance's version either
*is* the latest tag/SHA or it *isn't*. This module only ever answers "does
the running version match the latest known release", with no I/O and no
knowledge of :mod:`collapsarr.update_check.client` or the database.

Both channels are now a straight identity comparison of the whole tag string,
differing only in the tag prefix. The stable channel
(:func:`is_up_to_date`/:func:`running_version_tag`) compares ``v<version>``
against a fetched release's ``tag_name``. The beta channel
(:func:`is_up_to_date_beta`/:func:`running_beta_version_tag`, COL-96) compares
``beta-v<version>`` -- the beta workflow (``.github/workflows/beta.yml``) now
stamps the running ``__version__`` with a fully-orderable
``<base>.<build>+beta`` release+local segment (e.g. ``0.2.1.0007+beta``) and
tags each beta Release ``beta-v<base>.<build>`` (e.g. ``beta-v0.2.1.0007``)
using the *same* base+build identity, so the running version's public part
maps directly onto its Release tag. The old scheme (COL-88) embedded a git
short-SHA and needed to be un-embedded on both sides before comparing; the
build number carries the identity now, so that extraction is gone.
"""

from __future__ import annotations

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
    """Return the beta Release tag identity of a running beta ``__version__``.

    ``f"beta-v{public_version(version)}"`` -- the exact form
    :func:`is_up_to_date_beta` compares against a fetched prerelease's
    ``tag_name`` (``beta-v<base>.<build>``). Beta counterpart of
    :func:`running_version_tag`, dropping the ``+beta`` local segment first so
    the running version's public part lines up with its Release tag.
    """
    return f"{BETA_TAG_PREFIX}{public_version(version)}"


def is_up_to_date_beta(current_version: str, latest_tag: str | None) -> bool:
    """Return whether the running beta version matches the latest prerelease tag.

    Beta-channel counterpart of :func:`is_up_to_date`: still pure identity
    comparison (no semver ordering), now comparing the whole
    ``beta-v<base>.<build>`` tag (:func:`running_beta_version_tag`) rather than
    a buried short-SHA (COL-96 replaced the SHA scheme with an orderable
    base+build one -- see the module docstring).

    A ``None`` ``latest_tag`` (no successful fetch yet) reports "not up to
    date", same as :func:`is_up_to_date`. A **stable** running version (no
    :data:`~collapsarr.settings.models.BETA_LOCAL_SEGMENT_PREFIX` marker) also
    reports "not up to date" when checked against the beta channel -- there is
    no beta identity to compare, so this is the documented "switch to beta,
    see 'update available' with nothing to compare yet" edge case (COL-88's
    acceptance criteria), not a bug.
    """
    if latest_tag is None:
        return False
    if BETA_LOCAL_SEGMENT_PREFIX not in current_version:
        return False
    return running_beta_version_tag(current_version) == latest_tag


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
