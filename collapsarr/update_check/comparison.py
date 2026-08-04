"""Pure version-identity comparison for the stable and beta channels (COL-86, COL-88).

Deliberately **not** semver ordering: the running instance's version either
*is* the latest tag/SHA or it *isn't*. This module only ever answers "does
the running version match the latest known release", with no I/O and no
knowledge of :mod:`collapsarr.update_check.client` or the database.

The stable channel (:func:`is_up_to_date`) compares the whole tag string by
exact identity. The beta channel (:func:`is_up_to_date_beta`, COL-88) instead
compares just the embedded short-SHA: the beta workflow
(``.github/workflows/beta.yml``) stamps the running ``__version__`` with a
``+beta.<short-sha>`` local segment and tags each beta Release
``beta-<short-sha>`` using the *same* short-SHA, so two beta builds of the
same base version differ only in that suffix -- comparing the whole tag would
never match even when the SHAs are identical.
"""

from __future__ import annotations

from collapsarr.settings.models import BETA_LOCAL_SEGMENT_PREFIX

VERSION_TAG_PREFIX = "v"
"""Tags this repo's release workflow cuts are prefixed with this (``v1.2.3``),
matching the git tag pattern documented in ``docs/TRACKER.md`` /
``CONTEXT.md``'s Release Channel entry (``v*.*.*`` on ``main``)."""

BETA_TAG_PREFIX = "beta-"
"""The beta channel's GitHub Release tag prefix: the same workflow tags each
beta Release ``beta-<short-sha>`` (see ``.github/workflows/beta.yml``'s
``github-release`` job), using the identical short-SHA as
:data:`BETA_LOCAL_SEGMENT_PREFIX` above."""


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


def extract_beta_sha(version: str) -> str | None:
    """Return the short-SHA embedded in a running version's beta local segment.

    E.g. ``extract_beta_sha("1.2.3+beta.abc1234") == "abc1234"``. Returns
    ``None`` when ``version`` carries no :data:`BETA_LOCAL_SEGMENT_PREFIX` --
    a stable build's version string never contains it -- or when the segment
    is present but the SHA after it is empty (a malformed version string).
    """
    index = version.find(BETA_LOCAL_SEGMENT_PREFIX)
    if index == -1:
        return None
    sha = version[index + len(BETA_LOCAL_SEGMENT_PREFIX) :]
    return sha or None


def extract_beta_tag_sha(tag: str) -> str | None:
    """Return the short-SHA suffix of a beta prerelease tag (``beta-<sha>``).

    Returns ``None`` when ``tag`` doesn't start with :data:`BETA_TAG_PREFIX`
    (e.g. it's a stable ``v*.*.*`` tag) or the suffix after the prefix is
    empty (a malformed tag).
    """
    if not tag.startswith(BETA_TAG_PREFIX):
        return None
    sha = tag[len(BETA_TAG_PREFIX) :]
    return sha or None


def is_up_to_date_beta(current_version: str, latest_tag: str | None) -> bool:
    """Return whether the running version's embedded beta SHA matches the latest prerelease tag's.

    Beta-channel counterpart of :func:`is_up_to_date`: still pure identity
    comparison (no semver ordering), but comparing just the embedded
    short-SHA (:func:`extract_beta_sha`) against the latest prerelease tag's
    ``beta-<sha>`` suffix (:func:`extract_beta_tag_sha`) instead of the whole
    tag string -- see the module docstring for why.

    A ``None`` ``latest_tag`` (no successful fetch yet) reports "not up to
    date", same as :func:`is_up_to_date`. A **stable** running version (no
    embedded beta SHA) also reports "not up to date" when checked against the
    beta channel -- there is no SHA to compare, so this is the documented
    "switch to beta, see 'update available' with nothing to compare yet"
    edge case (COL-88's acceptance criteria), not a bug.
    """
    if latest_tag is None:
        return False
    current_sha = extract_beta_sha(current_version)
    if current_sha is None:
        return False
    latest_sha = extract_beta_tag_sha(latest_tag)
    if latest_sha is None:
        return False
    return current_sha == latest_sha


__all__ = [
    "BETA_LOCAL_SEGMENT_PREFIX",
    "BETA_TAG_PREFIX",
    "VERSION_TAG_PREFIX",
    "extract_beta_sha",
    "extract_beta_tag_sha",
    "is_up_to_date",
    "is_up_to_date_beta",
    "running_version_tag",
]
