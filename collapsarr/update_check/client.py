"""HTTP client for fetching the latest GitHub Release (COL-86).

Unauthenticated: GitHub's public releases endpoint doesn't need a token for
reasonable, infrequent (24h-cadence) polling of a public repo, so this client
never sends credentials. Mirrors :mod:`collapsarr.arr.client` /
:mod:`collapsarr.notify.dispatch`'s conventions -- never raises, network
errors/timeouts/non-2xx/malformed payloads are all captured as a failed
:class:`GitHubReleaseResult` so the reconcile step can persist a "no result"
outcome unconditionally, without a try/except of its own.

``GET /repos/<owner>/<repo>/releases/latest`` is GitHub's own "latest
release" endpoint, and it already excludes prereleases and drafts by
definition -- exactly the "latest non-prerelease tag" the stable channel's
version-identity comparison (:mod:`collapsarr.update_check.comparison`) needs,
with no extra filtering required here. Fetching the *beta* channel's latest
prerelease is a separate, later slice (a different endpoint --
``GET /repos/<owner>/<repo>/releases`` filtered to ``prerelease=true`` --
plus the semver-ordering comparison the ticket for COL-86 explicitly defers).

Tests inject a ``transport`` (``httpx.MockTransport``) instead of making real
network calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

GITHUB_REPO = "JovinJovinsson/Collapsarr"
_LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_DEFAULT_TIMEOUT = 10.0
_ERROR_BODY_LIMIT = 500
_ACCEPT_HEADER = "application/vnd.github+json"


@dataclass(frozen=True, slots=True)
class GitHubReleaseResult:
    """Outcome of fetching the latest GitHub Release.

    ``ok=False`` is the "no result" signal the ticket's spec calls for --
    network error, timeout, non-2xx, or a malformed/incomplete payload -- with
    ``error`` carrying a short human-readable reason for logging. On success,
    ``tag``/``name``/``body``/``published_at`` are GitHub's ``tag_name``/
    ``name``/``body``/``published_at`` fields verbatim (``name``/``body`` may
    legitimately be empty strings; only a missing/non-string ``tag_name`` or
    an unparsable ``published_at`` fails the result).
    """

    ok: bool
    tag: str | None = None
    name: str | None = None
    body: str | None = None
    published_at: datetime | None = None
    error: str | None = None


def fetch_latest_release(
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> GitHubReleaseResult:
    """Fetch the latest stable (non-prerelease, non-draft) GitHub Release.

    Never raises: every failure mode -- connection error, timeout, non-2xx,
    invalid JSON, or a payload missing the fields this needs -- is captured as
    a failed :class:`GitHubReleaseResult` instead of propagating, so callers
    (the scheduler's reconcile step) can persist an outcome unconditionally.
    """
    client = (
        httpx.Client(timeout=timeout, transport=transport)
        if transport is not None
        else httpx.Client(timeout=timeout)
    )

    try:
        with client:
            response = client.get(_LATEST_RELEASE_URL, headers={"Accept": _ACCEPT_HEADER})
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code}: {exc.response.text}"[:_ERROR_BODY_LIMIT]
        return GitHubReleaseResult(ok=False, error=detail)
    except httpx.HTTPError as exc:
        return GitHubReleaseResult(ok=False, error=str(exc))

    try:
        payload = response.json()
    except ValueError:
        return GitHubReleaseResult(ok=False, error="Invalid JSON in releases/latest response")

    if not isinstance(payload, dict):
        return GitHubReleaseResult(ok=False, error="releases/latest response was not an object")

    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        return GitHubReleaseResult(ok=False, error="releases/latest response missing 'tag_name'")

    name = payload.get("name")
    name = name if isinstance(name, str) else None
    body = payload.get("body")
    body = body if isinstance(body, str) else None

    published_at: datetime | None = None
    raw_published_at = payload.get("published_at")
    if isinstance(raw_published_at, str) and raw_published_at:
        try:
            published_at = datetime.fromisoformat(raw_published_at.replace("Z", "+00:00"))
        except ValueError:
            return GitHubReleaseResult(
                ok=False, error=f"Unparsable 'published_at': {raw_published_at!r}"
            )

    return GitHubReleaseResult(
        ok=True, tag=tag, name=name, body=body, published_at=published_at
    )


__all__ = ["GITHUB_REPO", "GitHubReleaseResult", "fetch_latest_release"]
