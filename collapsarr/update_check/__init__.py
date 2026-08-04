"""Update Check scheduler & persistence (COL-86, COL-89, part of Epic COL-85).

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
  :func:`dismiss_update_check` / :func:`undismiss_update_check` (COL-89)
  acknowledge/clear the current "update available" notice -- refusing (via
  :class:`UpdateNotAvailableError`) to dismiss when
  :func:`is_update_available` is false, mirroring
  :mod:`collapsarr.health.service`'s ``HealthCheckNotFailingError`` guard; a
  ``latest_tag`` transition fires a :data:`EVENT_UPDATE_AVAILABLE`
  notification via :func:`collapsarr.notify.dispatch_notification` and
  auto-clears any dismissal, mirroring
  :mod:`collapsarr.health.service`'s pass/fail pattern.
- **The GitHub Releases client** -- :func:`fetch_latest_release` /
  :func:`fetch_latest_prerelease` + :class:`GitHubReleaseResult`
  (:mod:`~collapsarr.update_check.client`): unauthenticated, never-raising
  fetches of the stable channel's latest release / the beta channel's latest
  prerelease (COL-88) respectively.
- **Version-identity comparison** -- :func:`is_up_to_date` /
  :func:`is_up_to_date_beta` (:mod:`~collapsarr.update_check.comparison`): pure,
  I/O-free match/no-match checks against the stable / beta channel (COL-88;
  no semver ordering either way).
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
- **Install-method detection** -- :func:`is_docker_environment`
  (:mod:`~collapsarr.update_check.environment`, COL-90): a ``/.dockerenv``
  presence probe, surfaced as ``is_docker`` on ``GET /api/system/updates`` so
  the frontend renders the matching Docker vs. pipx/pip upgrade instructions
  without any detection logic of its own (see ``docs/adr/
  0001-update-check-detect-notify-only.md``).

The Updates page and the app-wide "update available" indicator are COL-87's
frontend half (``frontend/src/pages/UpdatesPage.tsx``,
``frontend/src/components/UpdateIndicator.tsx``).
"""

from __future__ import annotations

from .client import GITHUB_REPO, GitHubReleaseResult, fetch_latest_prerelease, fetch_latest_release
from .comparison import (
    BETA_LOCAL_SEGMENT_PREFIX,
    BETA_TAG_PREFIX,
    VERSION_TAG_PREFIX,
    extract_beta_sha,
    extract_beta_tag_sha,
    is_up_to_date,
    is_up_to_date_beta,
    running_version_tag,
)
from .environment import DOCKERENV_PATH, is_docker_environment
from .models import UPDATE_CHECK_STATE_ID, UpdateCheckState
from .scheduler import INTERVAL_SECONDS, UpdateCheckScheduler
from .service import (
    EVENT_UPDATE_AVAILABLE,
    UpdateCheckStateNotFoundError,
    UpdateNotAvailableError,
    dismiss_update_check,
    get_update_check_state,
    is_update_available,
    reconcile_update_check,
    undismiss_update_check,
)

__all__ = [
    "BETA_LOCAL_SEGMENT_PREFIX",
    "BETA_TAG_PREFIX",
    "DOCKERENV_PATH",
    "EVENT_UPDATE_AVAILABLE",
    "GITHUB_REPO",
    "INTERVAL_SECONDS",
    "UPDATE_CHECK_STATE_ID",
    "VERSION_TAG_PREFIX",
    "GitHubReleaseResult",
    "UpdateCheckScheduler",
    "UpdateCheckState",
    "UpdateCheckStateNotFoundError",
    "UpdateNotAvailableError",
    "dismiss_update_check",
    "extract_beta_sha",
    "extract_beta_tag_sha",
    "fetch_latest_prerelease",
    "fetch_latest_release",
    "get_update_check_state",
    "is_docker_environment",
    "is_up_to_date",
    "is_up_to_date_beta",
    "is_update_available",
    "reconcile_update_check",
    "running_version_tag",
    "undismiss_update_check",
]
