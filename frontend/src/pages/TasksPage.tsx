import { ListChecks } from "lucide-react";
import { useEffect, useState } from "react";

import { fetchTasks, runBackup, runHealthChecks, runLibraryScan, runUpdateCheck } from "../api/tasks";
import type { ScheduledTask } from "../types/tasks";

/** Formats an ISO timestamp in the viewer's local time, or an em dash when absent/unparseable. */
function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; tasks: ScheduledTask[] };

/**
 * Maps a Scheduled Task's `name` (from `GET /api/system/tasks`) to its
 * existing manual-trigger endpoint (COL-122) -- each row's "Run" action
 * reuses that task's endpoint unchanged rather than a new per-task trigger
 * route (see `collapsarr/system/tasks.py`'s module docstring).
 */
const TASK_RUNNERS: Record<string, () => Promise<void>> = {
  "Library scan": runLibraryScan,
  "Health checks": runHealthChecks,
  Backups: runBackup,
  "Update check": runUpdateCheck,
};

/**
 * The System → Tasks view (COL-122): the Scheduled Task registry -- one row
 * per background scheduler (library scan, health checks, backups, update
 * check; `CONTEXT.md`'s "Scheduled Task") with its cadence, next execution
 * time, and a manual "Run" action, sourced from `GET /api/system/tasks`
 * (`fetchTasks`, `collapsarr/system/tasks.py`).
 *
 * "Next Execution" reads `—` when the server reports `next_run_at: null`:
 * that happens either because the task's scheduler isn't currently running
 * (`scheduler_enabled: false`) or it hasn't completed a run yet this
 * process -- the row still shows its configured `interval_label` either way,
 * so an operator always knows the cadence even before/without a run.
 *
 * Each row's "Run" action calls that task's own existing manual-trigger
 * endpoint (`api/tasks.ts`) -- none of those responses carry the full,
 * refreshed task list, so (unlike `HealthChecksPage`'s "Recheck now") a run
 * always follows up with a full `fetchTasks()` refetch, the same pattern
 * `BackupsPage`'s "Backup now" uses.
 */
export function TasksPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [actionError, setActionError] = useState<string | null>(null);
  const [runningName, setRunningName] = useState<string | null>(null);

  async function load() {
    try {
      const tasks = await fetchTasks();
      setState({ status: "ready", tasks });
    } catch (error: unknown) {
      setState({
        status: "error",
        message: error instanceof Error ? error.message : "Unknown error.",
      });
    }
  }

  useEffect(() => {
    let cancelled = false;
    fetchTasks()
      .then((tasks) => {
        if (!cancelled) {
          setState({ status: "ready", tasks });
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

  async function handleRun(task: ScheduledTask) {
    const runner = TASK_RUNNERS[task.name];
    if (!runner) return;
    setRunningName(task.name);
    setActionError(null);
    try {
      await runner();
      await load();
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : `Failed to run ${task.name}.`);
    } finally {
      setRunningName(null);
    }
  }

  return (
    <section className="view">
      <header className="view__header view__header--row">
        <div>
          <h1 className="view__title">Tasks</h1>
          <p className="view__summary">
            Every background Scheduled Task Collapsarr runs on a cadence — library scan, health
            checks, backups, and the update check — with its next execution time and a manual
            trigger.
          </p>
        </div>
      </header>

      {actionError && <p className="view__error">{actionError}</p>}

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading tasks…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <ListChecks width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load tasks: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && state.tasks.length === 0 && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <ListChecks width={28} height={28} />
          </span>
          <p className="panel__message">No scheduled tasks are registered.</p>
        </div>
      )}

      {state.status === "ready" && state.tasks.length > 0 && (
        <div className="panel">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Interval</th>
                <th scope="col">Next Execution</th>
                <th scope="col">Run</th>
              </tr>
            </thead>
            <tbody>
              {state.tasks.map((task) => {
                const isRunning = runningName === task.name;
                return (
                  <tr key={task.name}>
                    <td>{task.name}</td>
                    <td>{task.interval_label}</td>
                    <td>{formatTimestamp(task.next_run_at)}</td>
                    <td className="data-table__actions">
                      <button
                        type="button"
                        className="btn btn--secondary btn--sm"
                        onClick={() => handleRun(task)}
                        disabled={isRunning}
                      >
                        {isRunning ? "Running…" : "Run now"}
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
