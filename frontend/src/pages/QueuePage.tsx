import { useEffect, useMemo, useState } from "react";

import { bumpJobToFront, cancelJob, fetchJobQueue } from "../api/activity";
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

/** A per-row action `QueuePage` (COL-180) can run. */
type RowActionKind = "bump" | "cancel";

/**
 * Which per-row action is currently in flight, keyed by `job_id` (COL-180).
 * A single row can only have one action in flight at a time (its own two
 * buttons disable together while either is pending), but different rows'
 * actions are independent -- clicking "Cancel" on one row while another
 * row's "Process next" is still in flight doesn't touch the first row's
 * pending state, unlike a single shared "currently pending job" slot would.
 */
type PendingActions = Partial<Record<string, RowActionKind>>;

/**
 * A message surfaced after a per-row action settles (COL-180). `"hint"` is
 * the "too late" race outcome -- `bumped`/`cancelled` came back `false`,
 * which the backend documents as a normal, non-error outcome (the Job
 * simply started running, or finished, before the request landed) -- styled
 * the same muted way `FileDetailPage`'s skipped-trigger outcome is.
 * `"error"` is a genuine failure (network error, `404`, etc).
 */
type ActionNotice = { tone: "hint" | "error"; text: string } | null;

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
 * COL-180 adds per-row "Process next" (`bumpJobToFront`, `POST
 * /api/jobs/{job_id}/bump`, COL-169) and "Cancel" (`cancelJob`, `DELETE
 * /api/jobs/{job_id}`, COL-168) actions to every `pending` row -- `running`
 * rows get neither, there's nothing to reorder or cancel once a worker has
 * claimed a Job. Both endpoints report a would-be "too late" race (the Job
 * started running, or finished, between render and click) as a normal
 * `bumped`/`cancelled: false` result rather than an error, so a click that
 * loses that race surfaces a muted inline notice instead of leaving the row
 * looking broken or stuck; a genuine error (e.g. the Job vanished
 * entirely -- `404`) surfaces as a page-level error notice instead. Either
 * way the queue is re-fetched immediately after the action settles, rather
 * than waiting on the next scheduled poll, so the row's fate (moved to
 * front / removed / unaffected) is reflected right away. No confirm dialog
 * -- these are single-item, easily-reversible actions per the plan (only
 * page-level bulk actions, COL-181, get a confirm dialog).
 */
export function QueuePage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [fileFilter, setFileFilter] = useState("");
  const [pendingActions, setPendingActions] = useState<PendingActions>({});
  const [actionNotice, setActionNotice] = useState<ActionNotice>(null);

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

  /**
   * Best-effort immediate refresh, used right after a per-row action
   * settles so its outcome (row moved to front / removed / unaffected) is
   * reflected without waiting for the next scheduled poll. Deliberately
   * swallows its own failure -- the regular poll loop above already owns
   * surfacing/retrying a broken queue fetch; a refresh failure here would
   * otherwise stomp on the {@link ActionNotice} the action itself just set.
   */
  async function refreshQueueSoon(): Promise<void> {
    try {
      const entries = await fetchJobQueue();
      setState({ status: "ready", entries });
    } catch {
      // Swallowed -- see doc comment above.
    }
  }

  /**
   * Shared shape behind both per-row actions (COL-180): mark the row
   * pending, clear any stale notice, run `call` (resolving `true` on
   * success, `false` on the documented "too late" race), surface whichever
   * outcome happened, clear the row's pending state, then refresh. Only the
   * verb (`kind`), the request itself, and the two message strings differ
   * between "Process next" and "Cancel".
   */
  async function runRowAction(
    entry: JobHistoryEntry,
    kind: RowActionKind,
    call: () => Promise<boolean>,
    tooLateText: string,
    failureFallback: string,
  ): Promise<void> {
    setPendingActions((prev) => ({ ...prev, [entry.job_id]: kind }));
    setActionNotice(null);
    try {
      const succeeded = await call();
      if (!succeeded) {
        setActionNotice({ tone: "hint", text: tooLateText });
      }
    } catch (error) {
      setActionNotice({
        tone: "error",
        text: error instanceof Error ? error.message : failureFallback,
      });
    } finally {
      setPendingActions((prev) => {
        const next = { ...prev };
        delete next[entry.job_id];
        return next;
      });
    }
    await refreshQueueSoon();
  }

  async function handleProcessNext(entry: JobHistoryEntry): Promise<void> {
    await runRowAction(
      entry,
      "bump",
      async () => (await bumpJobToFront(entry.job_id)).bumped,
      `"${titleFromPath(entry.file_path)}" already started running before it could be moved to the front.`,
      "Failed to move job to the front of the queue.",
    );
  }

  async function handleCancel(entry: JobHistoryEntry): Promise<void> {
    await runRowAction(
      entry,
      "cancel",
      async () => (await cancelJob(entry.job_id)).cancelled,
      `"${titleFromPath(entry.file_path)}" already started running before it could be cancelled.`,
      "Failed to cancel job.",
    );
  }

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

      {actionNotice && (
        <p className={actionNotice.tone === "error" ? "view__error" : "form-hint"}>{actionNotice.text}</p>
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
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((entry) => {
                const isPendingRow = entry.status === "pending";
                const rowAction = pendingActions[entry.job_id] ?? null;
                return (
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
                    <td className="data-table__actions">
                      {isPendingRow && (
                        <>
                          <button
                            type="button"
                            className="btn btn--secondary btn--sm"
                            onClick={() => void handleProcessNext(entry)}
                            disabled={rowAction !== null}
                          >
                            {rowAction === "bump" ? "Processing…" : "Process next"}
                          </button>
                          <button
                            type="button"
                            className="btn btn--danger btn--sm"
                            onClick={() => void handleCancel(entry)}
                            disabled={rowAction !== null}
                          >
                            {rowAction === "cancel" ? "Cancelling…" : "Cancel"}
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
