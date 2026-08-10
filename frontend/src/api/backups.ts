import type { Backup, BackupList } from "../types/backups";
import { apiErrorMessage, apiFetch } from "./client";
import { prefixPath } from "../runtime/urlBase";

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
 * via a real browser navigation, not a `fetch`.
 *
 * `backup.id` is already the `<type>/<filename>` path segment the server
 * expects (see `types/backups.ts`), so it's interpolated directly rather than
 * `encodeURIComponent`-ed -- encoding its embedded `/` would break the route.
 *
 * COL-138: this used to route through `apiFetch`, read the whole response as
 * a `Blob`, then "click" a transient object-URL anchor -- the standard way to
 * save a *fetched* resource. But `response.blob()` buffers the entire archive
 * in tab memory before anything downloads, which silently stalls/OOMs on a
 * large backup, and because the download is JS-driven rather than a real
 * network navigation, the browser's download manager never sees it -- no
 * progress, no failure entry, nothing. That `fetch` was only needed to attach
 * the `X-Api-Key` header, but since COL-50 every `/api` request is *actually*
 * authenticated by the session cookie first (`client.ts`'s own docs), which
 * the browser attaches automatically to a plain navigation too -- so a direct
 * `<a href>` click authenticates exactly the same way, and lets the browser
 * stream the archive straight to disk with real progress/error reporting.
 */
export function downloadBackup(backup: Pick<Backup, "id" | "name">): void {
  const link = document.createElement("a");
  link.href = prefixPath(`/api/system/backup/${backup.id}/download`);
  link.download = backup.name;
  document.body.appendChild(link);
  link.click();
  link.remove();
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

/**
 * Restores the database from an *uploaded* archive
 * (`POST /api/system/backup/restore/upload`, COL-73).
 *
 * Unlike `restoreBackup` (which names a backup already on disk), this lets an
 * operator recover on a fresh box whose `backups/` folder is gone by uploading
 * an archive they downloaded earlier. The chosen `.zip` is sent as the raw
 * request body -- the server streams and size-caps it, then runs it through the
 * *same* hardened validate -> stage -> marker -> self-shutdown pipeline as the
 * listed restore (including the version-compatibility guard). On success
 * (`202`) the server has staged the database and is shutting down to apply it on
 * next boot, so this resolving is the cue to show the "restarting" state rather
 * than refresh the list. An oversize upload (`413`), a gate/extraction failure
 * (`422`), or any other error is surfaced via `apiErrorMessage`, same as the
 * per-row restore.
 */
export async function restoreFromUpload(file: File): Promise<void> {
  const response = await apiFetch("/api/system/backup/restore/upload", {
    method: "POST",
    body: file,
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to restore from upload (${response.status})`));
  }
}
