import { useEffect, useMemo, useState } from "react";

import { fetchJobHistory, requeueFile } from "../api/activity";
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

/**
 * Only the two terminal statuses this page ever shows (COL-176) -- unlike
 * the old combined Activity table's `STATUS_OPTIONS`, there is no
 * Pending/Running option here: those live rows moved to `QueuePage`
 * (COL-178), so every row on this page is already `succeeded` or `failed`.
 */
const HISTORY_STATUS_OPTIONS: Array<"succeeded" | "failed"> = ["succeeded", "failed"];

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
 * Which row's "Requeue" action is currently in flight, keyed by `job_id`.
 * A given failed row can only have one requeue in flight at a time -- its
 * own button disables while pending -- but different rows are independent.
 */
type PendingRequeues = Partial<Record<string, boolean>>;

/**
 * Result notice for a "Requeue" click (COL-176, COL-170). `"success"` is a
 * job actually enqueued. `"hint"` is the documented non-error skip outcome
 * (`enqueued: false` -- the file already has an active job, is unprobeable,
 * or has no qualifying target left) styled the same muted way `QueuePage`'s
 * "too late" race notice is. `"error"` is a genuine failure (network error,
 * non-ok response, etc).
 */
type RequeueNotice = { tone: "success" | "hint" | "error"; text: string } | null;

/**
 * The History view (COL-176): every terminal (`succeeded`/`failed`) Job,
 * newest first -- this is the old combined Activity table's content, minus
 * the live `pending`/`running` rows, which moved to the dedicated Queue view
 * in COL-178. Sourced from `GET /api/jobs/history` (COL-29,
 * `fetchJobHistory`), the same endpoint the old `ActivityPage` used --
 * unaffected by COL-175's additive `priority` field -- fetched unfiltered
 * and filtered client-side to terminal rows, same as that page's own
 * file/status filtering did (see `fetchJobHistory`'s doc comment for why
 * this doesn't lean on the endpoint's server-side `file`/`status` params).
 *
 * Filtering by file path (substring, case-insensitive) and status happens
 * client-side over the single fetched list, mirroring the old page exactly
 * except the status filter is narrowed to {@link HISTORY_STATUS_OPTIONS} --
 * there is no Pending/Running option since a row here is never anything
 * else.
 *
 * COL-176 adds a per-row "Requeue" action (`requeueFile`, `POST
 * /api/jobs/requeue`, COL-170) to every `failed` row -- a single explicit
 * action per the plan, so unlike `QueuePage`'s page-level "Clear queue"
 * (COL-181) this fires immediately with no confirm dialog, mirroring
 * `QueuePage`'s own per-row "Process next"/"Cancel" actions (COL-180). The
 * requeue creates a brand-new Job/history row rather than mutating the
 * clicked row, so the failed row this page shows stays exactly as it was --
 * a page-level notice (see {@link RequeueNotice}) reports the outcome
 * instead of the row itself changing, and there is nothing to re-fetch.
 */
export function HistoryPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [fileFilter, setFileFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "succeeded" | "failed">("all");
  const [expandedError, setExpandedError] = useState<JobHistoryEntry | null>(null);
  const [pendingRequeues, setPendingRequeues] = useState<PendingRequeues>({});
  const [requeueNotice, setRequeueNotice] = useState<RequeueNotice>(null);

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

  /**
   * Every terminal (`succeeded`/`failed`) row, newest first. `fetchJobHistory`
   * returns oldest-first (matching the backend's insertion-order query,
   * which `FileDetailPage`'s per-target status resolution depends on) --
   * reversed here, display-only, mirroring the old `ActivityPage`.
   */
  const terminalEntries = useMemo(() => {
    if (state.status !== "ready") return [];
    return [...state.entries].reverse().filter((entry) => entry.status === "succeeded" || entry.status === "failed");
  }, [state]);

  const filtered = useMemo(() => {
    const needle = fileFilter.trim().toLowerCase();
    return terminalEntries.filter((entry) => {
      const matchesFile = needle === "" || entry.file_path.toLowerCase().includes(needle);
      const matchesStatus = statusFilter === "all" || entry.status === statusFilter;
      return matchesFile && matchesStatus;
    });
  }, [terminalEntries, fileFilter, statusFilter]);

  const hasEntries = terminalEntries.length > 0;

  async function handleRequeue(entry: JobHistoryEntry): Promise<void> {
    setPendingRequeues((prev) => ({ ...prev, [entry.job_id]: true }));
    setRequeueNotice(null);
    try {
      const result = await requeueFile(entry.file_path);
      if (result.enqueued) {
        setRequeueNotice({
          tone: "success",
          text: `Requeued "${titleFromPath(entry.file_path)}" for processing.`,
        });
      } else {
        setRequeueNotice({
          tone: "hint",
          text: `"${titleFromPath(entry.file_path)}" could not be requeued right now (already queued or running, unprobeable, or nothing left to do).`,
        });
      }
    } catch (error) {
      setRequeueNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "Failed to requeue file.",
      });
    } finally {
      setPendingRequeues((prev) => {
        const next = { ...prev };
        delete next[entry.job_id];
        return next;
      });
    }
  }

  return (
    <section className="view">
      <header className="view__header">
        <h1 className="view__title">History</h1>
        <p className="view__summary">History of completed downmix jobs — succeeded and failed.</p>
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
            onChange={(event) => setStatusFilter(event.target.value as "all" | "succeeded" | "failed")}
          >
            <option value="all">All statuses</option>
            {HISTORY_STATUS_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {STATUS_LABEL[option]}
              </option>
            ))}
          </select>
        </div>
      )}

      {requeueNotice && (
        <p className={requeueNotice.tone === "error" ? "view__error" : "view__notice"} role="status">
          {requeueNotice.text}
        </p>
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

      {state.status === "ready" && !hasEntries && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <ActivityIcon width={28} height={28} />
          </span>
          <p className="panel__message">
            No history yet. Completed downmix jobs will be listed here once they succeed or fail.
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
                <th scope="col">Actions</th>
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
                  <td className="data-table__actions">
                    {entry.status === "failed" && (
                      <button
                        type="button"
                        className="btn btn--secondary btn--sm"
                        onClick={() => void handleRequeue(entry)}
                        disabled={pendingRequeues[entry.job_id] === true}
                      >
                        {pendingRequeues[entry.job_id] ? "Requeuing…" : "Requeue"}
                      </button>
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
