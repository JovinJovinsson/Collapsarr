import type { Backup, BackupList } from "../types/backups";
import { apiErrorMessage, apiFetch } from "./client";

/**
 * Fetches the backup list and support state (`GET /api/system/backup`, COL-63).
 *
 * Routed through `apiFetch` (COL-33's `client.ts`) so the session cookie / API
 * key rides along, same as every other `/api` call.
 */
export async function fetchBackups(): Promise<BackupList> {
  const response = await apiFetch("/api/system/backup");
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load backups (${response.status})`));
  }
  return (await response.json()) as BackupList;
}

/**
 * Creates a manual backup now (`POST /api/system/backup`, COL-63). Resolves
 * with the created backup's summary (the endpoint returns `202`).
 */
export async function createBackup(): Promise<Backup> {
  const response = await apiFetch("/api/system/backup", { method: "POST" });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to create backup (${response.status})`));
  }
  return (await response.json()) as Backup;
}
