import { useEffect, useMemo, useState } from "react";

import { fetchJobQueue } from "../api/activity";
import { ActivityIcon } from "../components/icons";
import { JOB_KIND_LABEL } from "../types/activity";
import type { JobHistoryEntry, JobStatus } from "../types/activity";

const STATUS_LABEL: Record<JobStatus, string> = {
  pending: "Pending",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
};

/** How often to re-poll `GET /api/jobs/queue` while the queue has running/pending content. */
const POLL_INTERVAL_ACTIVE_MS = 5_000;

/**
 * How often to re-poll once the queue is empty. Slower rather than stopped
 * outright -- there's still no push mechanism, so a fully-stopped poll would
 * never notice the scanner (or a manual trigger) enqueuing new work without
 * a manual reload.
 */
const POLL_INTERVAL_IDLE_MS = 20_000;

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
 * The live Queue view (COL-178, repurposing the old combined Activity/History
 * page): every currently `running`/`pending` Job, sourced from `GET
 * /api/jobs/queue` (COL-175, `fetchJobQueue`) -- running Jobs first, then
 * pending Jobs in ascending `priority` order (the queue's actual processing
 * order), exactly as the server returns them. No client-side re-sort.
 *
 * Terminal (`succeeded`/`failed`) rows -- what the old Activity table used to
 * show alongside the live ones -- move to a dedicated History page (COL-176);
 * this view only ever renders `pending`/`running` rows, so there's no status
 * filter here (unlike the old table, every row already shares one of two
 * live states) and no error/exit-code/ended columns (always empty for a
 * non-terminal row).
 *
 * Polls automatically -- there's no push mechanism, and a live queue view is
 * pointless if the operator has to keep manually reloading to see it move.
 * Polls every {@link POLL_INTERVAL_ACTIVE_MS} while the last fetch returned
 * any rows, or every {@link POLL_INTERVAL_IDLE_MS} once it's empty (see that
 * constant's doc comment for why idle polling slows rather than stops). A
 * failed poll surfaces the error but keeps retrying at the idle cadence
 * rather than giving up for good.
 *
 * No per-row or page-level actions yet (COL-180 adds "Process next"/"Cancel"
 * per pending row; COL-181 adds "Clear queue" and the auto-queue pause
 * toggle) -- this is read-only.
 */
export function QueuePage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [fileFilter, setFileFilter] = useState("");

  useEffect(() => {
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      try {
        const entries = await fetchJobQueue();
        if (cancelled) return;
        setState({ status: "ready", entries });
        const delay = entries.length > 0 ? POLL_INTERVAL_ACTIVE_MS : POLL_INTERVAL_IDLE_MS;
        timeoutId = setTimeout(() => void poll(), delay);
      } catch (error) {
        if (cancelled) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "Unknown error.",
        });
        // A transient failure shouldn't permanently stop the live view --
        // keep retrying at the idle cadence; the next successful poll clears
        // the error state.
        timeoutId = setTimeout(() => void poll(), POLL_INTERVAL_IDLE_MS);
      }
    }

    void poll();

    return () => {
      cancelled = true;
      if (timeoutId) clearTimeout(timeoutId);
    };
  }, []);

  const filtered = useMemo(() => {
    if (state.status !== "ready") return [];
    const needle = fileFilter.trim().toLowerCase();
    if (needle === "") return state.entries;
    return state.entries.filter((entry) => entry.file_path.toLowerCase().includes(needle));
  }, [state, fileFilter]);

  const hasEntries = state.status === "ready" && state.entries.length > 0;

  return (
    <section className="view">
      <header className="view__header">
        <h1 className="view__title">Queue</h1>
        <p className="view__summary">
          Live view of running and pending downmix jobs — refreshes automatically.
        </p>
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
        </div>
      )}

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading queue…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <ActivityIcon width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load queue: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && state.entries.length === 0 && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <ActivityIcon width={28} height={28} />
          </span>
          <p className="panel__message">
            Queue is empty. Running and pending downmix jobs will appear here.
          </p>
        </div>
      )}

      {hasEntries && filtered.length === 0 && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <ActivityIcon width={28} height={28} />
          </span>
          <p className="panel__message">No queued jobs match the current filter.</p>
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
                <th scope="col">Target</th>
                <th scope="col">Language</th>
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
                  <td>{entry.target ?? "—"}</td>
                  <td>{entry.language ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
