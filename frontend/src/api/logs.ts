import type { LogFile, LogFileList, LogLevelFilter, LogsResponse } from "../types/logs";
import { apiErrorMessage, apiFetch } from "./client";

/** Options for {@link fetchLogs}, all optional -- an empty call fetches the default tail window, unfiltered. */
export interface FetchLogsOptions {
  /** Minimum-severity filter (e.g. `"WARNING"` returns WARNING and ERROR lines); omit/`null` for no filtering. */
  level?: LogLevelFilter | null;
  /** Pages further back through the file -- pass a prior response's `next_offset`. */
  offset?: number;
}

/**
 * Fetches a tail window of the current log file (`GET /api/system/logs`,
 * COL-131) -- the most recent ~200 lines by default, newest last. `level`
 * applies minimum-severity filtering server-side (scanned while reading the
 * file, not post-filtered), and `offset` pages further back through it --
 * driving the System > Logs page's "Load older" action.
 */
export async function fetchLogs(options: FetchLogsOptions = {}): Promise<LogsResponse> {
  const params = new URLSearchParams();
  if (options.level) params.set("level", options.level);
  if (options.offset) params.set("offset", String(options.offset));
  const query = params.toString();

  const response = await apiFetch(`/api/system/logs${query ? `?${query}` : ""}`);
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load logs (${response.status})`));
  }
  return (await response.json()) as LogsResponse;
}

/**
 * Fetches the list of every file under `logs/` -- current + rotated backups
 * (`GET /api/system/logs/files`, COL-132), newest first.
 */
export async function fetchLogFiles(): Promise<LogFileList> {
  const response = await apiFetch("/api/system/logs/files");
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load log files (${response.status})`));
  }
  return (await response.json()) as LogFileList;
}

/**
 * Downloads one log file (`GET /api/system/logs/files/{name}/download`,
 * COL-132) and saves it to disk via the browser's download flow.
 *
 * Same blob + transient object-URL anchor pattern as `downloadBackup`
 * (`api/backups.ts`) -- a plain `<a href="...">` can't carry the stored API
 * key / session, so this routes the request through `apiFetch` (same auth as
 * every other call), reads the response as a `Blob`, and "clicks" a
 * transient object-URL anchor with `download` set.
 */
export async function downloadLogFile(file: Pick<LogFile, "name">): Promise<void> {
  const response = await apiFetch(`/api/system/logs/files/${file.name}/download`);
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to download log file (${response.status})`));
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = file.name;
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}

/**
 * Deletes every log file and recreates an empty current file
 * (`DELETE /api/system/logs`, COL-132). Destructive -- the caller gates this
 * behind a confirmation step (`LogsPage`'s "Clear logs" action) before
 * calling it. Resolves with no body on success (`204`).
 */
export async function clearLogs(): Promise<void> {
  const response = await apiFetch("/api/system/logs", { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to clear logs (${response.status})`));
  }
}
