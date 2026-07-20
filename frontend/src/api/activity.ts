import type { JobHistoryEntry } from "../types/activity";
import { apiErrorMessage, apiFetch } from "./client";

/**
 * Fetches persisted job history (`GET /api/jobs/history`, COL-29).
 *
 * The endpoint accepts optional `file` (exact path match) and `status`
 * query params for server-side filtering, but this fetches the full list
 * unfiltered and leaves filtering to the caller (`ActivityPage`): the
 * server's `file` filter is exact-match only, a poor fit for an
 * interactive text filter, and a single unparameterised request keeps this
 * consistent with `fetchWantedList`'s "one request, mocked once" shape for
 * tests.
 *
 * Uses a relative URL -- per `frontend/README.md`, the backend eventually
 * serves this bundle from its own origin, so no base URL is needed. Routed
 * through `apiFetch` (COL-33's `client.ts`) so the `X-Api-Key` header
 * (COL-26) rides along, same as every other `/api` call.
 */
export async function fetchJobHistory(): Promise<JobHistoryEntry[]> {
  const response = await apiFetch("/api/jobs/history");
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load job history (${response.status})`));
  }
  return (await response.json()) as JobHistoryEntry[];
}
