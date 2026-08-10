import { useEffect, useState } from "react";

import { clearLogs, downloadLogFile, fetchLogFiles, fetchLogs } from "../api/logs";
import { LogsIcon } from "../components/icons";
import type { LogFile, LogLevelFilter, LogsResponse } from "../types/logs";
import { formatBytes } from "../utils/format";

const LEVEL_OPTIONS: LogLevelFilter[] = ["DEBUG", "INFO", "WARNING", "ERROR"];

/** Selecting "All levels" fetches unfiltered -- distinct from any real `LogLevelFilter` value. */
const ALL_LEVELS = "";

type ReadyState = Extract<LoadState, { status: "ready" }>;

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; entries: LogsResponse["entries"]; nextOffset: number | null };

type FileListState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; files: LogFile[] };

/**
 * Builds the "ready" state from a fetched page, optionally prepending it
 * before entries already on screen (COL-131 code review: shared by the
 * mount effect, "Refresh", and "Load older" rather than each re-assembling
 * the same shape inline).
 */
function toReadyState(page: LogsResponse, existingEntries: ReadyState["entries"] = []): ReadyState {
  return { status: "ready", entries: [...page.entries, ...existingEntries], nextOffset: page.next_offset };
}

/** Formats an ISO timestamp in the viewer's local time, or the raw value if unparseable. */
function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

/**
 * The System → Logs view (COL-131/COL-132).
 *
 * The first section is a tail window of the *current* rotating log file
 * (`<data_dir>/logs/collapsarr.log`, COL-128) -- the most recent ~200 lines by
 * default, newest last -- sourced from `GET /api/system/logs` (`fetchLogs`,
 * `collapsarr/system/logs.py`).
 *
 * The minimum-severity `level` filter is applied server-side (re-fetches on
 * change, replacing the current window rather than filtering client-side),
 * matching the endpoint's "scanned server-side" contract. Like every other
 * System page (Health/Backups/Tasks), this never auto-polls -- "Refresh"
 * re-fetches the current tail window, and "Load older" pages further back
 * through the file (via the response's `next_offset`), prepending onto the
 * entries already shown rather than replacing them.
 *
 * COL-132 adds a second "Log files" section: every file under `logs/`
 * (current + rotated), fetched independently via `GET /api/system/logs/files`
 * (`fetchLogFiles`), mirroring `BackupsPage`'s list+download pattern -- a
 * per-row "Download" action (`downloadLogFile`, same blob/object-URL flow as
 * `downloadBackup`) and a page-level "Clear logs" action gated behind an
 * inline confirmation (it deletes every log file, current and rotated).
 * A successful clear re-fetches both the file list and the tail window, since
 * the current file it was showing has just been recreated empty.
 */
export function LogsPage() {
  const [level, setLevel] = useState<LogLevelFilter | typeof ALL_LEVELS>(ALL_LEVELS);
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [refreshing, setRefreshing] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [fileListState, setFileListState] = useState<FileListState>({ status: "loading" });
  const [downloadingName, setDownloadingName] = useState<string | null>(null);
  const [confirmingClear, setConfirmingClear] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [filesError, setFilesError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    setActionError(null);
    fetchLogs({ level: level || null })
      .then((page) => {
        if (!cancelled) {
          setState(toReadyState(page));
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({
            status: "error",
            message: error instanceof Error ? error.message : "Unknown error.",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [level]);

  async function loadFiles() {
    try {
      const list = await fetchLogFiles();
      setFileListState({ status: "ready", files: list.files });
    } catch (error: unknown) {
      setFileListState({
        status: "error",
        message: error instanceof Error ? error.message : "Unknown error.",
      });
    }
  }

  useEffect(() => {
    let cancelled = false;
    fetchLogFiles()
      .then((list) => {
        if (!cancelled) {
          setFileListState({ status: "ready", files: list.files });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setFileListState({
            status: "error",
            message: error instanceof Error ? error.message : "Unknown error.",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleRefresh() {
    setRefreshing(true);
    setActionError(null);
    try {
      const page = await fetchLogs({ level: level || null });
      setState(toReadyState(page));
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Failed to refresh logs.");
    } finally {
      setRefreshing(false);
    }
  }

  async function handleLoadOlder() {
    if (state.status !== "ready" || state.nextOffset === null) return;
    setLoadingOlder(true);
    setActionError(null);
    try {
      const page = await fetchLogs({ level: level || null, offset: state.nextOffset });
      setState((current) =>
        current.status === "ready" ? toReadyState(page, current.entries) : current
      );
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Failed to load older log lines.");
    } finally {
      setLoadingOlder(false);
    }
  }

  async function handleDownloadFile(file: LogFile) {
    setDownloadingName(file.name);
    setFilesError(null);
    try {
      await downloadLogFile(file);
    } catch (error: unknown) {
      setFilesError(error instanceof Error ? error.message : "Failed to download log file.");
    } finally {
      setDownloadingName(null);
    }
  }

  async function handleConfirmClear() {
    setClearing(true);
    setFilesError(null);
    try {
      await clearLogs();
      setConfirmingClear(false);
      // The current file was just deleted and recreated empty -- refresh
      // both the file list and the tail window so neither shows stale data.
      await Promise.all([loadFiles(), handleRefresh()]);
    } catch (error: unknown) {
      setFilesError(error instanceof Error ? error.message : "Failed to clear logs.");
    } finally {
      setClearing(false);
    }
  }

  const canLoadOlder = state.status === "ready" && state.nextOffset !== null;

  return (
    <section className="view">
      <header className="view__header view__header--row">
        <div>
          <h1 className="view__title">Logs</h1>
          <p className="view__summary">
            The current log file&apos;s most recent lines — oldest at the top, filterable by
            minimum severity.
          </p>
        </div>
        <div className="view__actions">
          <button
            type="button"
            className="btn btn--secondary"
            onClick={handleRefresh}
            disabled={refreshing || state.status === "loading"}
          >
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </header>

      <div className="logs-filters">
        <label className="visually-hidden" htmlFor="logs-level-filter">
          Minimum severity
        </label>
        <select
          id="logs-level-filter"
          className="logs-filters__select"
          value={level}
          onChange={(event) => setLevel(event.target.value as LogLevelFilter | typeof ALL_LEVELS)}
        >
          <option value={ALL_LEVELS}>All levels</option>
          {LEVEL_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {option}+
            </option>
          ))}
        </select>
      </div>

      {actionError && <p className="view__error">{actionError}</p>}

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading logs…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <LogsIcon width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load logs: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && state.entries.length === 0 && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <LogsIcon width={28} height={28} />
          </span>
          <p className="panel__message">
            {level ? `No ${level}+ log lines found.` : "The log file is empty."}
          </p>
        </div>
      )}

      {state.status === "ready" && state.entries.length > 0 && (
        <div className="panel">
          <table className="data-table logs-table">
            <thead>
              <tr>
                <th scope="col" className="logs-table__level-col">Level</th>
                <th scope="col" className="logs-table__line-col">Line</th>
              </tr>
            </thead>
            <tbody>
              {state.entries.map((entry, index) => (
                // `line_number` is only unique within a single response (COL-131 review:
                // the backend re-reads the file fresh per request, so a rotation between
                // "Refresh"/"Load older" calls can reuse a low line_number for different
                // content). `entries` here is always a fresh concatenation built by
                // `toReadyState` -- never reordered or spliced in place -- so the render
                // index is stable and collision-free for this list.
                <tr key={index}>
                  <td>
                    {entry.level && (
                      <span className={`logs-table__level logs-table__level--${entry.level.toLowerCase()}`}>
                        {entry.level}
                      </span>
                    )}
                  </td>
                  <td className="logs-table__line">{entry.text}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="form-actions">
            <button
              type="button"
              className="btn btn--secondary btn--sm"
              onClick={handleLoadOlder}
              disabled={!canLoadOlder || loadingOlder}
            >
              {loadingOlder ? "Loading…" : canLoadOlder ? "Load older" : "No older lines"}
            </button>
          </div>
        </div>
      )}

      <header className="view__header view__header--row">
        <div>
          <h2 className="view__title">Log files</h2>
          <p className="view__summary">
            Every file under <code>logs/</code> — the current file and rotated backups.
          </p>
        </div>
        {!confirmingClear && (
          <div className="view__actions">
            <button
              type="button"
              className="btn btn--danger"
              onClick={() => setConfirmingClear(true)}
              disabled={clearing}
            >
              Clear logs
            </button>
          </div>
        )}
      </header>

      {confirmingClear && (
        <div className="panel view__confirm" role="status">
          <p>
            Delete every log file — the current file and every rotated backup — and start a
            fresh, empty log? This can&apos;t be undone.
          </p>
          <div className="form-actions">
            <button
              type="button"
              className="btn btn--danger"
              onClick={handleConfirmClear}
              disabled={clearing}
            >
              {clearing ? "Clearing…" : "Confirm clear"}
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => setConfirmingClear(false)}
              disabled={clearing}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {filesError && <p className="view__error">{filesError}</p>}

      {fileListState.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading log files…</p>
        </div>
      )}

      {fileListState.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <LogsIcon width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load log files: {fileListState.message}</p>
        </div>
      )}

      {fileListState.status === "ready" && fileListState.files.length === 0 && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <LogsIcon width={28} height={28} />
          </span>
          <p className="panel__message">No log files yet.</p>
        </div>
      )}

      {fileListState.status === "ready" && fileListState.files.length > 0 && (
        <div className="panel">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Size</th>
                <th scope="col">Last written</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {fileListState.files.map((file) => (
                <tr key={file.name}>
                  <td>{file.name}</td>
                  <td>{formatBytes(file.size)}</td>
                  <td>{formatTimestamp(file.modified_at)}</td>
                  <td className="data-table__actions">
                    <button
                      type="button"
                      className="btn btn--secondary btn--sm"
                      onClick={() => handleDownloadFile(file)}
                      disabled={downloadingName === file.name}
                    >
                      {downloadingName === file.name ? "Downloading…" : "Download"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
