"""Update Check scheduler & persistence (COL-86, part of Epic COL-85).

The foundational, non-UI slice: the schema, GitHub client, comparison logic,
and scheduler everything else in the "Update detection & notifications" epic
builds on. Mirrors the Health Check Framework's scheduler/persistence pattern
(:mod:`collapsarr.health`) -- see ``CONTEXT.md``'s Release Channel / Update
Check entries for the domain vocabulary.

Public surface, by concern:

- **Persistence** -- :class:`UpdateCheckState` (:mod:`~collapsarr.update_check.models`);
  imported here so the model registers with :data:`collapsarr.database.Base.metadata`.
  :func:`get_update_check_state` / :func:`reconcile_update_check`
  (:mod:`~collapsarr.update_check.service`) read/write the singleton row.
- **The GitHub Releases client** -- :func:`fetch_latest_release` +
  :class:`GitHubReleaseResult` (:mod:`~collapsarr.update_check.client`): an
  unauthenticated, never-raising fetch of the stable channel's latest release.
- **Version-identity comparison** -- :func:`is_up_to_date`
  (:mod:`~collapsarr.update_check.comparison`): a pure, I/O-free match/no-match
  check against the stable channel (no semver ordering -- that's a later slice).
- **Orchestration** -- :class:`UpdateCheckScheduler`
  (:mod:`~collapsarr.update_check.scheduler`): a daemon-thread scheduler,
  structurally identical to :class:`~collapsarr.health.HealthCheckScheduler`,
  on a fixed 24h cadence.
- **API routes** -- ``GET /api/system/updates`` / ``POST /api/system/updates/
  recheck`` (:mod:`~collapsarr.update_check.routes`, COL-87): exposes the
  persisted state to an authenticated caller, mirroring
  :mod:`collapsarr.health.routes`'s shape. Mounted directly in
  :func:`collapsarr.main.create_app` (not re-exported here), same convention
  as ``health_checks_router``.

The Updates page and the app-wide "update available" indicator are COL-87's
frontend half (``frontend/src/pages/UpdatesPage.tsx``,
``frontend/src/components/UpdateIndicator.tsx``).
"""

from __future__ import annotations

from .client import GITHUB_REPO, GitHubReleaseResult, fetch_latest_release
from .comparison import VERSION_TAG_PREFIX, is_up_to_date, running_version_tag
from .models import UPDATE_CHECK_STATE_ID, UpdateCheckState
from .scheduler import INTERVAL_SECONDS, UpdateCheckScheduler
from .service import get_update_check_state, reconcile_update_check

__all__ = [
    "GITHUB_REPO",
    "INTERVAL_SECONDS",
    "UPDATE_CHECK_STATE_ID",
    "VERSION_TAG_PREFIX",
    "GitHubReleaseResult",
    "UpdateCheckScheduler",
    "UpdateCheckState",
    "fetch_latest_release",
    "get_update_check_state",
    "is_up_to_date",
    "reconcile_update_check",
    "running_version_tag",
]
