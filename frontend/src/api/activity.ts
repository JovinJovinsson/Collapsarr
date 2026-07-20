import type { JobHistoryEntry } from "../types/activity";

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
 * serves this bundle from its own origin, so no base URL is needed. The
 * `/api` prefix requires the `X-Api-Key` header (COL-26); the frontend
 * doesn't have anywhere to source/store that key yet, so it isn't sent here
 * -- that lands with the Settings/Connect view (COL-33), the natural home
 * for an API-key-aware fetch client the whole app can share.
 */
export async function fetchJobHistory(): Promise<JobHistoryEntry[]> {
  const response = await fetch("/api/jobs/history");
  if (!response.ok) {
    throw new Error(`Failed to load job history (${response.status})`);
  }
  return (await response.json()) as JobHistoryEntry[];
}
