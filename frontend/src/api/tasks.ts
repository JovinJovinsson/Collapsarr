import type { ScheduledTask } from "../types/tasks";
import { apiErrorMessage, apiFetch } from "./client";

/**
 * Fetches the Scheduled Task registry (`GET /api/system/tasks`, COL-122) --
 * one row per background scheduler (library scan, health checks, backups,
 * update check, Plex sync), driving the System > Tasks page.
 */
export async function fetchTasks(): Promise<ScheduledTask[]> {
  const response = await apiFetch("/api/system/tasks");
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load tasks (${response.status})`));
  }
  return (await response.json()) as ScheduledTask[];
}

/**
 * Triggers the Library Scan task's existing manual endpoint
 * (`POST /api/jobs/scan`, `collapsarr/jobs/routes.py::scan_now_endpoint`) --
 * COL-122 reuses it unchanged rather than adding a new per-task trigger
 * route. The response is a scan-result shape, not a task row, so callers
 * follow up with `fetchTasks()` to see the refreshed row.
 */
export async function runLibraryScan(): Promise<void> {
  const response = await apiFetch("/api/jobs/scan", { method: "POST" });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to run the library scan (${response.status})`)
    );
  }
}

/**
 * Triggers the Health Checks task's existing manual endpoint
 * (`POST /api/system/health-checks/recheck`,
 * `collapsarr/health/routes.py::recheck_health_checks_endpoint`, COL-83) --
 * reused unchanged by COL-122's "Run now" action.
 */
export async function runHealthChecks(): Promise<void> {
  const response = await apiFetch("/api/system/health-checks/recheck", { method: "POST" });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to run health checks (${response.status})`)
    );
  }
}

/**
 * Triggers the Backups task's existing manual endpoint
 * (`POST /api/system/backup`,
 * `collapsarr/backup/routes.py::create_backup_endpoint`, COL-63) -- reused
 * unchanged by COL-122's "Run now" action.
 */
export async function runBackup(): Promise<void> {
  const response = await apiFetch("/api/system/backup", { method: "POST" });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to run the backup (${response.status})`)
    );
  }
}

/**
 * Triggers the Update Check task's existing manual endpoint
 * (`POST /api/system/updates/recheck`,
 * `collapsarr/update_check/routes.py::recheck_updates_endpoint`, COL-87) --
 * reused unchanged by COL-122's "Run now" action.
 */
export async function runUpdateCheck(): Promise<void> {
  const response = await apiFetch("/api/system/updates/recheck", { method: "POST" });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to run the update check (${response.status})`)
    );
  }
}

/**
 * Triggers the Plex Sync task's manual endpoint
 * (`POST /api/plex/sync`, `collapsarr/plex/routes.py::run_plex_sync_endpoint`,
 * COL-210) -- the "Run now" action for the Plex Sync Scheduled Task. Rebuilds
 * the Plex Library Item mapping table; the response is a row-count summary, not
 * a task row, so callers follow up with `fetchTasks()` to see the refreshed row.
 */
export async function runPlexSync(): Promise<void> {
  const response = await apiFetch("/api/plex/sync", { method: "POST" });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to run the Plex sync (${response.status})`)
    );
  }
}
