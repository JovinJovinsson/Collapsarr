import type { UpdateCheckState } from "../types/updates";
import { apiErrorMessage, apiFetch } from "./client";

/**
 * Fetches the current Update Check state -- running vs. latest version,
 * changelog, when it was last checked, and whether an update is available
 * (`GET /api/system/updates`, COL-87). Authenticated, like every other
 * `/api` route -- backs both the System > Updates page and the app-wide
 * "update available" indicator.
 */
export async function fetchUpdateStatus(): Promise<UpdateCheckState> {
  const response = await apiFetch("/api/system/updates");
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to load update status (${response.status})`)
    );
  }
  return (await response.json()) as UpdateCheckState;
}

/**
 * Runs an Update Check tick immediately and returns the refreshed state
 * (`POST /api/system/updates/recheck`, COL-87). Triggers a real, out-of-band
 * tick on the server (not a scheduler restart), mirroring
 * `recheckHealthChecks` (`api/health.ts`) -- the response already carries the
 * full refreshed state, so no separate follow-up fetch is needed.
 */
export async function recheckUpdateStatus(): Promise<UpdateCheckState> {
  const response = await apiFetch("/api/system/updates/recheck", {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to run a manual update check (${response.status})`)
    );
  }
  return (await response.json()) as UpdateCheckState;
}
