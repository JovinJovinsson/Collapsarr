import { CakeSlice } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { triggerDownmix } from "../api/activity";
import { fetchWantedList } from "../api/wanted";
import type { JobStatus } from "../types/activity";
import type { WantedFile } from "../types/wanted";

const TARGET_LABEL: Record<string, string> = {
  stereo: "Stereo",
  "2.1": "2.1",
  "5.1": "5.1",
};

const STATUS_LABEL: Record<JobStatus, string> = {
  pending: "Pending",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
};

/** Best-effort display title from a file path: last segment, minus extension. */
function titleFromPath(filePath: string): string {
  const base = filePath.split(/[/\\]/).pop() || filePath;
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base;
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; files: WantedFile[] };

/**
 * Which rows' "Queue now" action (COL-195) is currently in flight, keyed by
 * `WantedFile.id`. Mirrors `QueuePage`'s (COL-180) `PendingActions` map --
 * different rows' actions are independent, so clicking one row doesn't
 * disable another's button -- simplified to a single action kind since this
 * page only has the one action (no bump/cancel pair to distinguish).
 */
type PendingIds = Partial<Record<number, true>>;

/**
 * Result notice for the per-row "Queue now" action (COL-195), shown
 * page-level above the table like `QueuePage`'s `ActionNotice`. Unlike that
 * page's bump/cancel actions -- whose outcome is visible immediately in the
 * (re-fetched) row itself -- queuing a file doesn't change anything about the
 * Wanted row (it stays wanted until the job completes), so `"success"` gets
 * its own explicit notice here rather than being silent. `"hint"` is the
 * skipped-file outcome (`enqueued: false`, e.g. already queued or
 * unprobeable) -- a documented normal outcome of `POST /api/jobs/trigger`,
 * not an error -- and `"error"` is a genuine failure (network error, `404`,
 * etc), styled the same way `QueuePage`'s notice distinguishes the two.
 */
type ActionNotice = { tone: "success" | "hint" | "error"; text: string } | null;

/**
 * The Wanted view (COL-31): every tracked file still missing at least one
 * enabled downmix target, sourced live from `GET /api/wanted` (COL-28).
 *
 * COL-195 adds a per-row "Queue now" action, calling the same manual-trigger
 * endpoint (`triggerDownmix`, `POST /api/jobs/trigger`, COL-29) the file
 * detail page's "Trigger downmix" button already uses, keyed on the row's
 * `file_path` -- letting a downmix be queued directly from the list without
 * navigating to the file's detail page first. No `extra_languages`
 * allow-list bypass here (that stays a detail-page-only input); this is
 * always a plain trigger honouring the configured allow-list.
 */
export function WantedPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [pendingIds, setPendingIds] = useState<PendingIds>({});
  const [actionNotice, setActionNotice] = useState<ActionNotice>(null);

  useEffect(() => {
    let cancelled = false;

    fetchWantedList()
      .then((files) => {
        if (!cancelled) {
          setState({ status: "ready", files });
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
   * Handles a row's "Queue now" click (COL-195): marks the row pending,
   * calls {@link triggerDownmix} with the row's `file_path`, and surfaces the
   * outcome as a page-level {@link ActionNotice} -- success (job enqueued),
   * hint (skipped -- a documented normal outcome, not an error), or error
   * (the request itself failed). Mirrors `QueuePage.runRowAction`'s shape,
   * minus the post-action refetch: unlike a queue row, a Wanted row doesn't
   * change as a direct result of queuing it.
   */
  async function handleQueueNow(file: WantedFile): Promise<void> {
    const title = titleFromPath(file.file_path);
    setPendingIds((prev) => ({ ...prev, [file.id]: true }));
    setActionNotice(null);
    try {
      const result = await triggerDownmix({ file_path: file.file_path });
      if (result.enqueued && result.job) {
        setActionNotice({
          tone: "success",
          text: `"${title}" queued — job ${result.job.id} (${STATUS_LABEL[result.job.status]}).`,
        });
      } else {
        setActionNotice({
          tone: "hint",
          text: `"${title}" wasn't queued — the file was skipped (already queued, unprobeable, or nothing qualifying).`,
        });
      }
    } catch (error) {
      setActionNotice({
        tone: "error",
        text: error instanceof Error ? error.message : `Failed to queue "${title}" for downmix.`,
      });
    } finally {
      setPendingIds((prev) => {
        const next = { ...prev };
        delete next[file.id];
        return next;
      });
    }
  }

  return (
    <section className="view">
      <header className="view__header">
        <h1 className="view__title">Wanted</h1>
        <p className="view__summary">Monitored files still missing an enabled downmix target.</p>
      </header>

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading wanted files…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <CakeSlice width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load the wanted list: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && state.files.length === 0 && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <CakeSlice width={28} height={28} />
          </span>
          <p className="panel__message">
            Nothing wanted right now. Every tracked file already has all enabled downmix targets.
          </p>
        </div>
      )}

      {actionNotice && (
        <p
          className={
            actionNotice.tone === "error"
              ? "view__error"
              : actionNotice.tone === "success"
                ? "view__notice"
                : "form-hint"
          }
          role="status"
        >
          {actionNotice.text}
        </p>
      )}

      {state.status === "ready" && state.files.length > 0 && (
        <div className="panel wanted-panel">
          <table className="wanted-table">
            <thead>
              <tr>
                <th scope="col">Title</th>
                <th scope="col">Path</th>
                <th scope="col">Missing targets</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {state.files.map((file) => {
                const isPending = pendingIds[file.id] === true;
                return (
                  <tr key={file.id}>
                    <td className="wanted-table__title">
                      <Link to={`/wanted/${file.id}`}>{titleFromPath(file.file_path)}</Link>
                    </td>
                    <td className="wanted-table__path">{file.file_path}</td>
                    <td>
                      {file.missing_targets.length === 0 ? (
                        <span className="wanted-table__none">—</span>
                      ) : (
                        <ul className="wanted-table__targets">
                          {file.missing_targets.map((missing) => (
                            <li key={`${missing.language}-${missing.target}`} className="panel__tag">
                              {missing.language} · {TARGET_LABEL[missing.target] ?? missing.target}
                            </li>
                          ))}
                        </ul>
                      )}
                    </td>
                    <td className="data-table__actions">
                      <button
                        type="button"
                        className="btn btn--secondary btn--sm"
                        onClick={() => void handleQueueNow(file)}
                        disabled={isPending}
                      >
                        {isPending ? "Queuing…" : "Queue now"}
                      </button>
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
