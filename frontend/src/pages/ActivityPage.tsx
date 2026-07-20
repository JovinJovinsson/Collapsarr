import { useEffect, useMemo, useState } from "react";

import { fetchJobHistory } from "../api/activity";
import { ActivityIcon } from "../components/icons";
import type { JobHistoryEntry, JobStatus } from "../types/activity";

const STATUS_LABEL: Record<JobStatus, string> = {
  pending: "Pending",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
};

const STATUS_OPTIONS: JobStatus[] = ["pending", "running", "succeeded", "failed"];

/** Best-effort display title from a file path: last segment, minus extension. */
function titleFromPath(filePath: string): string {
  const base = filePath.split(/[/\\]/).pop() || filePath;
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base;
}

/** Formats an ISO timestamp for display, or an em dash when absent/unset. */
function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; entries: JobHistoryEntry[] };

/**
 * The Activity/History view (COL-32): every persisted downmix job run,
 * sourced from `GET /api/jobs/history` (COL-29, `fetchJobHistory`).
 *
 * Filtering by file path (substring, case-insensitive) and status happens
 * client-side over the single fetched list -- see `fetchJobHistory` for why
 * this doesn't use the endpoint's `file`/`status` query params directly.
 */
export function ActivityPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [fileFilter, setFileFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | JobStatus>("all");

  useEffect(() => {
    let cancelled = false;

    fetchJobHistory()
      .then((entries) => {
        if (!cancelled) {
          setState({ status: "ready", entries });
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
  }, []);

  const filtered = useMemo(() => {
    if (state.status !== "ready") return [];
    const needle = fileFilter.trim().toLowerCase();
    return state.entries.filter((entry) => {
      const matchesFile = needle === "" || entry.file_path.toLowerCase().includes(needle);
      const matchesStatus = statusFilter === "all" || entry.status === statusFilter;
      return matchesFile && matchesStatus;
    });
  }, [state, fileFilter, statusFilter]);

  const hasEntries = state.status === "ready" && state.entries.length > 0;

  return (
    <section className="view">
      <header className="view__header">
        <h1 className="view__title">Activity</h1>
        <p className="view__summary">History of downmix jobs — queued, running, completed, and failed.</p>
      </header>

      {hasEntries && (
        <div className="activity-filters">
          <input
            type="search"
            className="activity-filters__input"
            placeholder="Filter by file path…"
            aria-label="Filter by file path"
            value={fileFilter}
            onChange={(event) => setFileFilter(event.target.value)}
          />
          <select
            className="activity-filters__select"
            aria-label="Filter by status"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value as "all" | JobStatus)}
          >
            <option value="all">All statuses</option>
            {STATUS_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {STATUS_LABEL[option]}
              </option>
            ))}
          </select>
        </div>
      )}

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading job history…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <ActivityIcon width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load job history: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && state.entries.length === 0 && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <ActivityIcon width={28} height={28} />
          </span>
          <p className="panel__message">
            No activity yet. Downmix jobs and their history will be listed here as they run.
          </p>
        </div>
      )}

      {hasEntries && filtered.length === 0 && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <ActivityIcon width={28} height={28} />
          </span>
          <p className="panel__message">No job history matches the current filters.</p>
        </div>
      )}

      {filtered.length > 0 && (
        <div className="panel activity-panel">
          <table className="activity-table">
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">Status</th>
                <th scope="col">Started</th>
                <th scope="col">Ended</th>
                <th scope="col">Exit code</th>
                <th scope="col">Target</th>
                <th scope="col">Language</th>
                <th scope="col">Error</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((entry) => (
                <tr key={entry.id}>
                  <td>
                    <div className="activity-table__title">{titleFromPath(entry.file_path)}</div>
                    <div className="activity-table__path">{entry.file_path}</div>
                  </td>
                  <td>
                    <span
                      className={`activity-table__status activity-table__status--${entry.status}`}
                    >
                      {STATUS_LABEL[entry.status]}
                    </span>
                  </td>
                  <td>{formatTimestamp(entry.started_at)}</td>
                  <td>{formatTimestamp(entry.ended_at)}</td>
                  <td>{entry.exit_code ?? "—"}</td>
                  <td>{entry.target ?? "—"}</td>
                  <td>{entry.language ?? "—"}</td>
                  <td className="activity-table__error">{entry.error_text ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
