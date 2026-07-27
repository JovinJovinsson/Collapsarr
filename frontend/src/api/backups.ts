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

/**
 * Downloads a backup archive (`GET /api/system/backup/{id}/download`, COL-64)
 * and saves it to disk via the browser's download flow.
 *
 * `backup.id` is already the `<type>/<filename>` path segment the server
 * expects (see `types/backups.ts`), so it's interpolated directly rather than
 * `encodeURIComponent`-ed -- encoding its embedded `/` would break the route.
 * A plain `<a href="...">` can't carry the stored API key / session, so this
 * routes the request through `apiFetch` (same auth as every other call),
 * reads the response as a `Blob`, and "clicks" a transient object-URL anchor
 * with `download` set -- the standard way to trigger a save dialog for a
 * fetched (rather than directly linked) resource.
 */
export async function downloadBackup(backup: Pick<Backup, "id" | "name">): Promise<void> {
  const response = await apiFetch(`/api/system/backup/${backup.id}/download`);
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to download backup (${response.status})`));
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = backup.name;
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}

/**
 * Deletes a backup archive (`DELETE /api/system/backup/{id}`, COL-65).
 *
 * `backup.id` is the `<type>/<filename>` path segment, interpolated directly
 * (not `encodeURIComponent`-ed) so its embedded `/` survives -- same as
 * `downloadBackup`. The server refuses with a `409` (surfaced via
 * `apiErrorMessage`) when the delete would drop below the minimum-keep floor,
 * and `404`s an unknown id.
 */
export async function deleteBackup(backup: Pick<Backup, "id">): Promise<void> {
  const response = await apiFetch(`/api/system/backup/${backup.id}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to delete backup (${response.status})`));
  }
}

/**
 * Restores the database from a listed backup
 * (`POST /api/system/backup/restore/{id}`, COL-71).
 *
 * `backup.id` is interpolated directly, same as `downloadBackup`/`deleteBackup`.
 * On success (`202`) the server has already staged the database and armed the
 * restore marker, then triggered its own shutdown -- the supervisor
 * (Docker/systemd) restarts the process and the swap applies on next boot, so
 * this call resolving is the caller's cue to show a "restarting" state rather
 * than refresh the list (the current process may already be on its way down).
 * A gate failure (`422`) or unknown id (`404`) is surfaced via
 * `apiErrorMessage`, same as the other row actions.
 */
export async function restoreBackup(backup: Pick<Backup, "id">): Promise<void> {
  const response = await apiFetch(`/api/system/backup/restore/${backup.id}`, { method: "POST" });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to restore backup (${response.status})`));
  }
}
