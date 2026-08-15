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

/**
 * Known values of `SelfUpdateStatus.phase` (COL-230, `collapsarr.self_update.
 * models.SELF_UPDATE_PHASES`) -- kept as a union for the polling screen's
 * (COL-231) phase-to-copy mapping, but `SelfUpdateStatus.phase` itself stays
 * typed as plain `string` below: the backend column is a plain `String`, not
 * a DB-level enum (see that module's docstring), so a value this vocabulary
 * hasn't caught up with yet must never be a type error, only fall through to
 * a generic default copy.
 *
 * `"idle"` is both "no attempt has ever run" and the terminal *success*
 * state once a completed attempt's post-re-exec health check passes;
 * `"rolled_back"` is the terminal *failure* state (COL-234, COL-236) --
 * `previous_version` names what it rolled back to.
 */
export type SelfUpdatePhase =
  | "idle"
  | "preparing"
  | "downloading"
  | "verifying"
  | "applying"
  | "awaiting_health"
  | "rolled_back";

/**
 * The singleton Self-Update attempt state, as returned by
 * `GET /api/system/self-update/status` (COL-230,
 * `collapsarr.self_update.routes.SelfUpdateStatusRead`) -- field-for-field
 * off `collapsarr.self_update.models.SelfUpdateState`. Polled by
 * `SelfUpdateProgress` (COL-231) across the app's restart/re-exec downtime
 * window until the freshly-booted process answers again.
 */
export interface SelfUpdateStatus {
  /** Whether an attempt is currently under way -- the guard `begin_self_update`/`clear_self_update` hold. */
  in_progress: boolean;
  /** The current step (see `SelfUpdatePhase`), or an as-yet-unrecognized value from a newer backend. */
  phase: SelfUpdatePhase | (string & {});
  /**
   * The version this attempt could roll back to (stamped when the attempt
   * began), or `null` before any self-update has ever run. Still populated
   * immediately after a `"rolled_back"` completion -- that's what a
   * "rolled back to vX.Y.Z" terminal message names.
   */
  previous_version: string | null;
}
