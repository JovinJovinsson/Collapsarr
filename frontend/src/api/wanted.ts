import type { WantedFile } from "../types/wanted";
import { apiErrorMessage, apiFetch } from "./client";

/**
 * Fetches the wanted list: every tracked file missing at least one
 * currently-enabled downmix target (`GET /api/wanted`, COL-28).
 *
 * Uses a relative URL -- per `frontend/README.md`, the backend eventually
 * serves this bundle from its own origin, so no base URL is needed. Routed
 * through `apiFetch` (COL-33's `client.ts`) so the `X-Api-Key` header
 * (COL-26) rides along, same as every other `/api` call.
 */
export async function fetchWantedList(): Promise<WantedFile[]> {
  const response = await apiFetch("/api/wanted");
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load wanted list (${response.status})`));
  }
  return (await response.json()) as WantedFile[];
}
