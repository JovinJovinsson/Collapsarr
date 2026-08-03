"""Pure version-identity comparison for the stable channel (COL-86).

Deliberately **not** semver ordering: the running instance's version either
*is* the latest tag or it *isn't*. Semver-aware "am I behind by how much"
comparison (needed once the beta channel's prerelease/local-segment tags,
e.g. ``v1.2.3+beta.abc1234``, enter the picture) is an explicitly separate,
later slice per this ticket's spec -- this module only ever answers "does the
running version match the latest tag", with no I/O and no knowledge of
:mod:`collapsarr.update_check.client` or the database.
"""

from __future__ import annotations

VERSION_TAG_PREFIX = "v"
"""Tags this repo's release workflow cuts are prefixed with this (``v1.2.3``),
matching the git tag pattern documented in ``docs/TRACKER.md`` /
``CONTEXT.md``'s Release Channel entry (``v*.*.*`` on ``main``)."""


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


__all__ = ["VERSION_TAG_PREFIX", "is_up_to_date", "running_version_tag"]
