import type { HealthCheckState, HealthStatus } from "../types/health";
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

/**
 * Fetches every registered check's current state, passing or failing
 * (`GET /api/system/health-checks`, COL-76). Authenticated, like every other
 * `/api` route -- backs the System > Health list page.
 */
export async function fetchHealthChecks(): Promise<HealthCheckState[]> {
  const response = await apiFetch("/api/system/health-checks");
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to load health checks (${response.status})`)
    );
  }
  return (await response.json()) as HealthCheckState[];
}

/**
 * Dismisses one currently-failing check state
 * (`POST /api/system/health-checks/{id}/dismiss`, COL-82). Hides it from the
 * `/health` banner while it stays visible, marked dismissed, on the System >
 * Health list page. The server refuses with a `409` (surfaced via
 * `apiErrorMessage`) when the check is not currently failing, and `404`s an
 * unknown id.
 */
export async function dismissHealthCheck(check: Pick<HealthCheckState, "id">): Promise<HealthCheckState> {
  const response = await apiFetch(`/api/system/health-checks/${check.id}/dismiss`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to dismiss health check (${response.status})`)
    );
  }
  return (await response.json()) as HealthCheckState;
}

/**
 * Clears a dismissal on one check state
 * (`POST /api/system/health-checks/{id}/undismiss`, COL-82). Always succeeds
 * regardless of the check's current passing/failing status; `404`s an
 * unknown id.
 */
export async function undismissHealthCheck(
  check: Pick<HealthCheckState, "id">
): Promise<HealthCheckState> {
  const response = await apiFetch(`/api/system/health-checks/${check.id}/undismiss`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to undismiss health check (${response.status})`)
    );
  }
  return (await response.json()) as HealthCheckState;
}

/**
 * Runs every registered health check immediately
 * (`POST /api/system/health-checks/recheck`, COL-83) and returns the
 * resulting full state -- the same shape `fetchHealthChecks` returns.
 * Triggers a real, out-of-band tick on the server (not a scheduler restart),
 * so a check that actually changed fires the usual edge-triggered
 * notification server-side; this call just surfaces the fresh result.
 */
export async function recheckHealthChecks(): Promise<HealthCheckState[]> {
  const response = await apiFetch("/api/system/health-checks/recheck", {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to run a manual recheck (${response.status})`)
    );
  }
  return (await response.json()) as HealthCheckState[];
}
