import { useEffect, useState } from "react";

import { fetchLogs } from "../api/logs";
import { LogsIcon } from "../components/icons";
import type { LogLevelFilter, LogsResponse } from "../types/logs";

const LEVEL_OPTIONS: LogLevelFilter[] = ["DEBUG", "INFO", "WARNING", "ERROR"];

/** Selecting "All levels" fetches unfiltered -- distinct from any real `LogLevelFilter` value. */
const ALL_LEVELS = "";

type ReadyState = Extract<LoadState, { status: "ready" }>;

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; entries: LogsResponse["entries"]; nextOffset: number | null };

/**
 * Builds the "ready" state from a fetched page, optionally prepending it
 * before entries already on screen (COL-131 code review: shared by the
 * mount effect, "Refresh", and "Load older" rather than each re-assembling
 * the same shape inline).
 */
function toReadyState(page: LogsResponse, existingEntries: ReadyState["entries"] = []): ReadyState {
  return { status: "ready", entries: [...page.entries, ...existingEntries], nextOffset: page.next_offset };
}

/**
 * The System → Logs view (COL-131): a tail window of the *current* rotating
 * log file (`<data_dir>/logs/collapsarr.log`, COL-128) -- the most recent
 * ~200 lines by default, newest last -- sourced from `GET /api/system/logs`
 * (`fetchLogs`, `collapsarr/system/logs.py`).
 *
 * The minimum-severity `level` filter is applied server-side (re-fetches on
 * change, replacing the current window rather than filtering client-side),
 * matching the endpoint's "scanned server-side" contract. Like every other
 * System page (Health/Backups/Tasks), this never auto-polls -- "Refresh"
 * re-fetches the current tail window, and "Load older" pages further back
 * through the file (via the response's `next_offset`), prepending onto the
 * entries already shown rather than replacing them.
 */
export function LogsPage() {
  const [level, setLevel] = useState<LogLevelFilter | typeof ALL_LEVELS>(ALL_LEVELS);
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [refreshing, setRefreshing] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

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
                <th scope="col">Level</th>
                <th scope="col">Line</th>
              </tr>
            </thead>
            <tbody>
              {state.entries.map((entry) => (
                <tr key={entry.line_number}>
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
    </section>
  );
}
