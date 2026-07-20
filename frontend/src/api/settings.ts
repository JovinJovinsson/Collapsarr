import type { GlobalSettings, GlobalSettingsUpdateInput } from "../types/settings";
import { apiErrorMessage, apiFetch } from "./client";

/** Fetches the global settings row (`GET /api/settings`, COL-28). */
export async function fetchSettings(): Promise<GlobalSettings> {
  const response = await apiFetch("/api/settings");
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load settings (${response.status})`));
  }
  return (await response.json()) as GlobalSettings;
}

/**
 * Updates the provided settings fields (`PUT /api/settings`, COL-28).
 * Only fields present on `input` are changed server-side -- see
 * `GlobalSettingsUpdateInput`'s partial-update contract.
 */
export async function updateSettings(input: GlobalSettingsUpdateInput): Promise<GlobalSettings> {
  const response = await apiFetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to update settings (${response.status})`));
  }
  return (await response.json()) as GlobalSettings;
}
