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

/**
 * Dismisses the current "update available" notice
 * (`POST /api/system/updates/dismiss`, COL-89). Mirrors `dismissHealthCheck`
 * (`api/health.ts`), but scoped to the singleton Update Check row -- no id
 * parameter, since there is only ever one.
 */
export async function dismissUpdateStatus(): Promise<UpdateCheckState> {
  const response = await apiFetch("/api/system/updates/dismiss", {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to dismiss the update notice (${response.status})`)
    );
  }
  return (await response.json()) as UpdateCheckState;
}

/**
 * Clears a dismissal on the current "update available" notice early
 * (`POST /api/system/updates/undismiss`, COL-89). Mirrors
 * `undismissHealthCheck` (`api/health.ts`).
 */
export async function undismissUpdateStatus(): Promise<UpdateCheckState> {
  const response = await apiFetch("/api/system/updates/undismiss", {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(
        response,
        `Failed to undismiss the update notice (${response.status})`
      )
    );
  }
  return (await response.json()) as UpdateCheckState;
}
