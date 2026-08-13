import type { PlexConnection, PlexConnectionUpdateInput } from "../types/plex";
import { apiErrorMessage, apiFetch } from "./client";

/** Fetches the singleton Plex connection row (`GET /api/plex/connection`, COL-209). */
export async function fetchPlexConnection(): Promise<PlexConnection> {
  const response = await apiFetch("/api/plex/connection");
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load Plex connection (${response.status})`));
  }
  return (await response.json()) as PlexConnection;
}

/**
 * Updates the Plex connection (`PUT /api/plex/connection`, COL-209) and
 * re-validates connectivity server-side. Only fields present on `input` are
 * changed -- see `PlexConnectionUpdateInput`'s partial-update contract.
 */
export async function updatePlexConnection(input: PlexConnectionUpdateInput): Promise<PlexConnection> {
  const response = await apiFetch("/api/plex/connection", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to update Plex connection (${response.status})`));
  }
  return (await response.json()) as PlexConnection;
}
