/**
 * Types mirroring the Scheduled Task registry (`collapsarr/system/tasks.py`)
 * -- kept in sync by hand since there's no shared schema generation yet.
 */

/**
 * One row of the Scheduled Task registry, as returned by
 * `GET /api/system/tasks` (COL-122,
 * `collapsarr/system/tasks.py::ScheduledTaskRead`) -- one per background
 * scheduler (library scan, health checks, backups, update check;
 * `CONTEXT.md`'s "Scheduled Task").
 */
export interface ScheduledTask {
  /** e.g. `"Library scan"`, `"Health checks"`, `"Backups"`, `"Update check"`. */
  name: string;
  /** Human-readable cadence, e.g. `"Every 6 hours"`. Always populated -- derived from configuration, not run state. */
  interval_label: string;
  /** ISO-8601 UTC; `null` when `scheduler_enabled` is `false` or this task hasn't completed a run yet. */
  next_run_at: string | null;
  /** ISO-8601 UTC; `null` until this task's scheduler has completed a run. */
  last_run_at: string | null;
  /** Whether this task's background loop is actually running right now. */
  scheduler_enabled: boolean;
}
