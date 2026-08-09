/**
 * Types mirroring the tail-read log endpoint (`collapsarr/system/logs.py`)
 * -- kept in sync by hand since there's no shared schema generation yet.
 */

/**
 * The four minimum-severity filter values, matching
 * `collapsarr.system.logs.LogLevelFilter` (the same four levels Settings ->
 * General's log-level dropdown exposes, `types/settings.ts`'s `LogLevel`).
 */
export type LogLevelFilter = "DEBUG" | "INFO" | "WARNING" | "ERROR";

/**
 * A parsed log line's severity, as reported in `LogEntry.level`. Widens
 * `LogLevelFilter` with `"CRITICAL"` -- not selectable as a filter value, but
 * a line logged at it can still appear in the results (e.g. under an
 * `ERROR` filter, which it satisfies).
 */
export type LogSeverity = LogLevelFilter | "CRITICAL";

/**
 * One line of the current log file, as returned by `GET /api/system/logs`
 * (COL-131, `collapsarr/system/logs.py::LogEntryRead`).
 */
export interface LogEntry {
  /** 1-based within this response's read of the file -- not a durable id across requests. */
  line_number: number;
  /**
   * The severity of the record this line belongs to -- inherited across a
   * multi-line record's continuation lines (e.g. a traceback), so every
   * line of one record reports the same level. `null` only for a line
   * preceding any recognisable record header.
   */
  level: LogSeverity | null;
  /** The raw log line, verbatim (already redacted at write time, COL-128/ADR-0006). */
  text: string;
}

/**
 * Response shape for `GET /api/system/logs` (COL-131). Named `LogsResponse`
 * -- not `LogsPage` -- to avoid colliding with the `LogsPage` view component
 * (`pages/LogsPage.tsx`) that renders it.
 */
export interface LogsResponse {
  /** Oldest first / newest last, matching how a terminal `tail` reads. */
  entries: LogEntry[];
  /** Pass as the next request's `offset` to page further back under the same `level` filter; `null` once there's nothing further back. */
  next_offset: number | null;
}

/**
 * One file under `logs/` -- the current file or a rotated backup
 * (`collapsarr.log`, `collapsarr.log.1`, ...), as returned by
 * `GET /api/system/logs/files` (COL-132, `collapsarr/system/logs.py::LogFileRead`).
 */
export interface LogFile {
  /** Bare filename -- the id the download endpoint's `{name}` path segment expects. */
  name: string;
  /** File size in bytes. */
  size: number;
  /** ISO-8601 UTC last-modified timestamp; displayed in local time. */
  modified_at: string;
}

/** `GET /api/system/logs/files` response (COL-132), newest first. */
export interface LogFileList {
  files: LogFile[];
}
