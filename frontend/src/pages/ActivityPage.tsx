import { useEffect, useMemo, useState } from "react";

import { fetchJobHistory } from "../api/activity";
import { ActivityIcon } from "../components/icons";
import { Modal } from "../components/Modal";
import { JOB_KIND_LABEL } from "../types/activity";
import type { JobHistoryEntry, JobStatus } from "../types/activity";

const STATUS_LABEL: Record<JobStatus, string> = {
  pending: "Pending",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
};

const STATUS_OPTIONS: JobStatus[] = ["pending", "running", "succeeded", "failed"];

/** Error text past this length (or containing a newline) gets truncated with a "Show more" modal. */
const ERROR_TRUNCATE_THRESHOLD = 160;

/** Best-effort display title from a file path: last segment, minus extension. */
function titleFromPath(filePath: string): string {
  const base = filePath.split(/[/\\]/).pop() || filePath;
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base;
}

/** Whether `text` is long/complex enough to warrant truncation + a "Show more" modal. */
function needsTruncation(text: string): boolean {
  return text.length > ERROR_TRUNCATE_THRESHOLD || text.includes("\n");
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
 * The Activity/History view (COL-32): every persisted downmix and
 * set-default-audio job run, sourced from `GET /api/jobs/history` (COL-29,
 * `fetchJobHistory`). The "Kind" column (COL-155's job history `kind`
 * field, COL-157) distinguishes a `DOWNMIX` row from a `SET_DEFAULT_AUDIO`
 * one -- including historical rows backfilled as `downmix` before COL-155.
 *
 * Filtering by file path (substring, case-insensitive) and status happens
 * client-side over the single fetched list -- see `fetchJobHistory` for why
 * this doesn't use the endpoint's `file`/`status` query params directly.
 */
export function ActivityPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [fileFilter, setFileFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | JobStatus>("all");
  const [expandedError, setExpandedError] = useState<JobHistoryEntry | null>(null);

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
    // `fetchJobHistory` returns oldest-first (matching the backend's
    // insertion-order query, which `FileDetailPage`'s per-target status
    // resolution depends on) -- reversed here, display-only, so the most
    // recently queued job (including one still pending/running, COL-108)
    // shows up at the top of the Activity table.
    return [...state.entries].reverse().filter((entry) => {
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
                <th scope="col">Kind</th>
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
                    <span className={`activity-table__kind activity-table__kind--${entry.kind}`}>
                      {JOB_KIND_LABEL[entry.kind]}
                    </span>
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
                  <td className="activity-table__error">
                    {entry.error_text ? (
                      needsTruncation(entry.error_text) ? (
                        <>
                          <p className="activity-table__error-text activity-table__error-text--clamped">
                            {entry.error_text}
                          </p>
                          <button
                            type="button"
                            className="activity-table__show-more"
                            onClick={() => setExpandedError(entry)}
                          >
                            Show more
                          </button>
                        </>
                      ) : (
                        <p className="activity-table__error-text">{entry.error_text}</p>
                      )
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {expandedError && (
        <Modal title={titleFromPath(expandedError.file_path)} onClose={() => setExpandedError(null)}>
          <pre className="activity-error-modal__text">{expandedError.error_text}</pre>
        </Modal>
      )}
    </section>
  );
}
