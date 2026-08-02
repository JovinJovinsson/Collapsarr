/**
 * Types mirroring the health-check surfaces (`collapsarr/health/*`) -- kept in
 * sync by hand since there's no shared schema generation yet.
 */

/** Exactly two levels, fixed per Check Code (`CONTEXT.md`'s "Check Severity"). */
export type HealthSeverity = "warning" | "error";

/** Whether a check's current tick is satisfied. */
export type HealthCheckStatus = "passing" | "failing";

/**
 * One failing check as surfaced by the unauthenticated `GET /health`
 * liveness probe (COL-38; `severity` added in COL-76). Minimal by design --
 * for every check's full detail (passing or failing), see `HealthCheckState`
 * below.
 */
export interface HealthWarning {
  code: string;
  message: string;
  severity: HealthSeverity;
}

/** The `/health` liveness probe's response shape. */
export interface HealthStatus {
  status: "ok" | "degraded";
  version: string;
  warnings: HealthWarning[];
}

/**
 * One persisted health-check state row, as returned in full by
 * `GET /api/system/health-checks` (COL-76,
 * `collapsarr/health/routes.py::HealthCheckStateRead`) -- every registered
 * check's current state, not just the currently-failing ones `/health`
 * surfaces.
 *
 * `id` is the row's stable database primary key -- kept here (rather than
 * keying only on `code`/`instance_id`) so a later slice can address one row
 * directly for an action, e.g. COL-82's dismiss/undismiss and COL-83's manual
 * recheck (both out of scope for this page today).
 */
export interface HealthCheckState {
  id: number;
  code: string;
  category: string;
  status: HealthCheckStatus;
  severity: HealthSeverity;
  message: string;
  /** `null` for a singleton check (FFmpeg, disk, database); an Arr instance id otherwise. */
  instance_id: number | null;
  /** ISO-8601 UTC; `null` unless this Check Key is (or was, until it recovered) failing. */
  first_failed_at: string | null;
  /** ISO-8601 UTC timestamp of the most recent tick that evaluated this check. */
  last_checked_at: string;
}
