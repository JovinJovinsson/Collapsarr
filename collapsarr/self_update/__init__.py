"""Self-update state, in-progress guard, checksum client, status endpoint
(COL-230, foundational slice of Epic COL-224 "Phase 2: In-app Self-Update
(Native & Pipx)").

The schema, checksum client, and guard everything else in this epic
(COL-232-236: the pipx/native apply flows, pause-restore, rollback, and the
frontend polling screen) builds on. Mirrors
:mod:`collapsarr.update_check`/:mod:`collapsarr.ffmpeg_download`'s module
shape and conventions -- see ``CONTEXT.md`` for the domain vocabulary this
epic introduces.

Public surface, by concern:

- **Persistence** -- :class:`SelfUpdateState` (:mod:`~collapsarr.self_update.
  models`); imported here so the model registers with
  :data:`collapsarr.database.Base.metadata`. :func:`get_self_update_state`
  reads (get-or-create) the singleton row.
- **In-progress guard** -- :func:`begin_self_update` (set, raising
  :class:`SelfUpdateAlreadyInProgressError` if already set),
  :func:`set_self_update_phase` (advance the current phase),
  :func:`clear_self_update` (clear) -- all in
  :mod:`~collapsarr.self_update.service`.
- **The SHA256SUMS checksum client** -- :func:`fetch_checksum_entry` (fetch +
  parse + resolve in one call), :func:`parse_sha256sums`,
  :func:`resolve_checksum_entry`, :func:`resolve_asset_filename`,
  :func:`resolve_platform_arch` (:mod:`~collapsarr.self_update.client`):
  unauthenticated, never-raising fetch of a release's ``SHA256SUMS`` asset
  (published by ``release.yml`` since COL-227) and resolution of the entry
  matching this process's platform/arch.
- **The pipx apply flow (COL-232)** -- :func:`apply_pipx_update`
  (:mod:`~collapsarr.self_update.apply`): the "no Jobs running" download
  -verify-upgrade-re-exec sequence for ``pipx`` installs, plus
  :func:`stable_update_target` (the shared "is a newer stable release even
  available" gate).
- **API routes** -- ``GET /api/system/self-update/status``,
  ``POST /api/system/self-update/apply`` (COL-232)
  (:mod:`~collapsarr.self_update.routes`): exposes the persisted state, and
  triggers the apply flow, to an authenticated caller, mirroring
  :mod:`collapsarr.update_check.routes`'s/:mod:`collapsarr.ffmpeg_download.
  routes`'s shape. Mounted directly in :func:`collapsarr.main.create_app`
  (not re-exported here), same convention as ``update_checks_router``.
"""

from __future__ import annotations

from .apply import SelfUpdateApplyOutcome, apply_pipx_update, stable_update_target
from .client import (
    SelfUpdateChecksumResult,
    fetch_checksum_entry,
    fetch_sha256sums,
    parse_sha256sums,
    resolve_asset_filename,
    resolve_checksum_entry,
    resolve_platform_arch,
)
from .models import (
    PHASE_APPLYING,
    PHASE_AWAITING_HEALTH,
    PHASE_DOWNLOADING,
    PHASE_IDLE,
    PHASE_ROLLED_BACK,
    PHASE_VERIFYING,
    SELF_UPDATE_PHASES,
    SELF_UPDATE_STATE_ID,
    SelfUpdateState,
)
from .service import (
    SelfUpdateAlreadyInProgressError,
    begin_self_update,
    clear_self_update,
    get_self_update_state,
    set_self_update_phase,
)

__all__ = [
    "PHASE_APPLYING",
    "PHASE_AWAITING_HEALTH",
    "PHASE_DOWNLOADING",
    "PHASE_IDLE",
    "PHASE_ROLLED_BACK",
    "PHASE_VERIFYING",
    "SELF_UPDATE_PHASES",
    "SELF_UPDATE_STATE_ID",
    "SelfUpdateAlreadyInProgressError",
    "SelfUpdateApplyOutcome",
    "SelfUpdateChecksumResult",
    "SelfUpdateState",
    "apply_pipx_update",
    "begin_self_update",
    "clear_self_update",
    "fetch_checksum_entry",
    "fetch_sha256sums",
    "get_self_update_state",
    "parse_sha256sums",
    "resolve_asset_filename",
    "resolve_checksum_entry",
    "resolve_platform_arch",
    "set_self_update_phase",
    "stable_update_target",
]
