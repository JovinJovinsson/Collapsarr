import type { WantedFile } from "../types/wanted";

/**
 * Fetches the wanted list: every tracked file missing at least one
 * currently-enabled downmix target (`GET /api/wanted`, COL-28).
 *
 * Uses a relative URL -- per `frontend/README.md`, the backend eventually
 * serves this bundle from its own origin, so no base URL is needed. The
 * `/api` prefix requires the `X-Api-Key` header (COL-26); the frontend
 * doesn't have anywhere to source/store that key yet, so it isn't sent here
 * -- that lands with the Settings/Connect view (COL-33), the natural home
 * for an API-key-aware fetch client the whole app can share.
 */
export async function fetchWantedList(): Promise<WantedFile[]> {
  const response = await fetch("/api/wanted");
  if (!response.ok) {
    throw new Error(`Failed to load wanted list (${response.status})`);
  }
  return (await response.json()) as WantedFile[];
}
