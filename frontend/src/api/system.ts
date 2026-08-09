import type { SystemInfo } from "../types/system";
import { apiErrorMessage, apiFetch } from "./client";

/**
 * Fetches the About panel's environment/runtime facts (`GET
 * /api/system/info`, COL-123), driving the System > Status page.
 */
export async function fetchSystemInfo(): Promise<SystemInfo> {
  const response = await apiFetch("/api/system/info");
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to load system info (${response.status})`)
    );
  }
  return (await response.json()) as SystemInfo;
}
