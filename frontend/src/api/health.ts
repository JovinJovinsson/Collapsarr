import type { HealthStatus } from "../types/health";
import { apiErrorMessage, apiFetch } from "./client";

/**
 * Fetches the app's health/liveness status (`GET /health`, COL-38). Unlike
 * every other module here, `/health` sits outside the `/api` prefix and is
 * intentionally unauthenticated (`collapsarr/auth.py`) -- `apiFetch` is still
 * used for consistency, but the API key it may attach is simply ignored.
 */
export async function fetchHealth(): Promise<HealthStatus> {
  const response = await apiFetch("/health");
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load health status (${response.status})`));
  }
  return (await response.json()) as HealthStatus;
}
