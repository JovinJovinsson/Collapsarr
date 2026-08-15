/**
 * Types mirroring the Update Check surface (`collapsarr/update_check/routes.py`,
 * COL-87) -- kept in sync by hand since there's no shared schema generation
 * yet, same convention as `types/health.ts`.
 */

import type { InstallMethod } from "./system";

/**
 * The singleton Update Check state, as returned by `GET /api/system/updates`
 * / `POST /api/system/updates/recheck` / `.../dismiss` / `.../undismiss`
 * (`collapsarr/update_check/routes.py::UpdateCheckStateRead`).
 *
 * `latest_version`/`latest_version_label`/`changelog`/`checked_at` are all
 * `null` before the very first tick has run (or, for the release fields,
 * before any tick has ever *succeeded* -- a failed fetch only advances
 * `checked_at`, see `collapsarr/update_check/service.py`). `update_available`
 * is computed server-side on every read, never persisted -- it always
 * reflects the running instance's version against the freshest known
 * release.
 */
export interface UpdateCheckState {
  /** The running instance's `collapsarr.__version__`, unprefixed (e.g. `"0.1.0"`). */
  running_version: string;
  /** The latest known release's tag (e.g. `"v1.2.3"`), or `null` before a successful fetch. */
  latest_version: string | null;
  /** The latest known release's human-readable name, or `null`. */
  latest_version_label: string | null;
  /** The latest known release's raw notes (plain text in this slice -- Markdown rendering is a later slice). */
  changelog: string | null;
  /** ISO-8601 UTC timestamp of the most recent tick (success or failure), or `null` before the first one. */
  checked_at: string | null;
  /** Whether the running version differs from the latest known release -- informational, not a failure state. */
  update_available: boolean;
  /**
   * ISO-8601 UTC timestamp of when an operator dismissed the current "update
   * available" notice (COL-89), or `null` if it hasn't been dismissed.
   * Automatically cleared server-side the next time `latest_version`
   * changes -- a dismissal only ever silences the *current* known release.
   */
  dismissed_at: string | null;
  /**
   * How this instance is installed (COL-90/COL-215), detected server-side
   * (`collapsarr/system/info.py::install_method` -- Docker via `/.dockerenv`,
   * else native via `sys.frozen`, else pipx) -- the frontend has no
   * filesystem access, so it can't detect this itself. Selects which
   * upgrade-instruction block `UpdatesPage` renders: `docker pull` +
   * recreate-container for `"docker"`, `pipx upgrade`/`pip install --upgrade`
   * for `"pipx"`, download-and-replace for `"native"`. Formerly the
   * `is_docker` boolean (COL-215 widened it to this three-valued field).
   */
  install_method: InstallMethod;
}

/**
 * Matches `collapsarr.self_update.routes.SelfUpdateApplyRequest`'s `flow`
 * literal (COL-233, COL-228). Picks how `POST /api/system/self-update/apply`
 * handles downmix Jobs currently `RUNNING` at trigger time --
 * `"cancel_and_restart"` hard-cancels and immediately requeues them
 * (bypassing the Recently-Processed Window); `"wait_and_restart"` blocks
 * until they finish naturally, cancelling nothing. See `CONTEXT.md`'s
 * **Self-Update** entry for the user-facing "Cancel & Restart Now"/"Wait &
 * Restart" labels these map to.
 */
export type SelfUpdateFlow = "cancel_and_restart" | "wait_and_restart";

/**
 * Request body for `POST /api/system/self-update/apply` (COL-232, COL-233,
 * COL-228, `collapsarr.self_update.routes.SelfUpdateApplyRequest`).
 *
 * `flow` is optional -- omit it (or send `{}`) when no Jobs are currently
 * running; the endpoint answers `409` if Jobs turn out to be running and no
 * `flow` was chosen, rather than guessing. `UpdatesPage`'s confirmation
 * modal (COL-228) always resolves which case applies *before* calling this
 * endpoint (via a `GET /api/jobs/queue` running-Job count), so it always
 * sends the correct `flow` up front rather than relying on that `409` retry
 * path.
 */
export interface SelfUpdateApplyRequest {
  flow?: SelfUpdateFlow;
}

/**
 * Response shape for a successful `POST /api/system/self-update/apply`
 * (COL-228, `collapsarr.self_update.routes.SelfUpdateApplyRead`). `ok` is
 * only ever actually observed in a test harness that injects a fake
 * re-exec/exit seam -- a real apply re-execs (pipx) or exits (native,
 * handing off to the staged-swap process) the running process outright, so
 * production never actually receives this response before the connection
 * drops.
 */
export interface SelfUpdateApplyResult {
  ok: boolean;
}
