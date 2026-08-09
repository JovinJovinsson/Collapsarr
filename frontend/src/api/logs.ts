import type { LogLevelFilter, LogsResponse } from "../types/logs";
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
