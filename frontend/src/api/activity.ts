import type { JobHistoryEntry, ManualTriggerRequest, ManualTriggerResult } from "../types/activity";
import { apiErrorMessage, apiFetch } from "./client";

const JSON_HEADERS = { "Content-Type": "application/json" };

/**
 * Fetches persisted job history (`GET /api/jobs/history`, COL-29).
 *
 * The endpoint accepts optional `file` (exact path match) and `status`
 * query params for server-side filtering. `ActivityPage` fetches the full
 * list unfiltered and filters client-side (the server's `file` filter is
 * exact-match only, a poor fit for its interactive text filter), but
 * `FileDetailPage` (COL-34) already knows the exact file path it wants
 * history for, so it passes `filePath` to use the server-side filter
 * directly instead of fetching and filtering the whole table.
 *
 * Uses a relative URL -- per `frontend/README.md`, the backend eventually
 * serves this bundle from its own origin, so no base URL is needed. Routed
 * through `apiFetch` (COL-33's `client.ts`) so the `X-Api-Key` header
 * (COL-26) rides along, same as every other `/api` call.
 */
export async function fetchJobHistory(filePath?: string): Promise<JobHistoryEntry[]> {
  const url = filePath ? `/api/jobs/history?file=${encodeURIComponent(filePath)}` : "/api/jobs/history";
  const response = await apiFetch(url);
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load job history (${response.status})`));
  }
  return (await response.json()) as JobHistoryEntry[];
}

/**
 * Manually enqueues a downmix job for one file (`POST /api/jobs/trigger`,
 * COL-29), used by `FileDetailPage`'s "Trigger downmix" action (COL-34).
 *
 * `extra_languages` is the allow-list-bypass option: languages listed there
 * are unioned onto the scheduler's `language_allow_list` for this one call,
 * letting a user downmix a language the global allow-list would otherwise
 * exclude. A `202` is returned whether or not a job was enqueued -- the
 * response's `enqueued` flag (not the HTTP status) distinguishes a queued
 * job from a skipped file, so this only throws on a genuine error response.
 */
export async function triggerDownmix(input: ManualTriggerRequest): Promise<ManualTriggerResult> {
  const response = await apiFetch("/api/jobs/trigger", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to trigger downmix (${response.status})`));
  }
  return (await response.json()) as ManualTriggerResult;
}
