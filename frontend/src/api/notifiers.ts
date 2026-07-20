import type { NotifierConfig, NotifierConfigUpdateInput } from "../types/notifiers";
import { apiErrorMessage, apiFetch } from "./client";

/** Fetches the notifier config row (`GET /api/notifiers`, COL-36). */
export async function fetchNotifierConfig(): Promise<NotifierConfig> {
  const response = await apiFetch("/api/notifiers");
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load notifier config (${response.status})`));
  }
  return (await response.json()) as NotifierConfig;
}

/**
 * Updates the provided notifier config fields (`PUT /api/notifiers`, COL-36).
 * Only fields present on `input` are changed server-side -- see
 * `NotifierConfigUpdateInput`'s partial-update contract.
 */
export async function updateNotifierConfig(
  input: NotifierConfigUpdateInput,
): Promise<NotifierConfig> {
  const response = await apiFetch("/api/notifiers", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to update notifier config (${response.status})`));
  }
  return (await response.json()) as NotifierConfig;
}
