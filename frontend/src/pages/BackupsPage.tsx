import { useEffect, useState } from "react";

import { createBackup, fetchBackups } from "../api/backups";
import { BackupIcon } from "../components/icons";
import type { Backup } from "../types/backups";

/** Formats an ISO timestamp in the viewer's local time, or the raw value if unparseable. */
function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

/** Human-readable byte size (e.g. `1.4 MB`). */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = bytes / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(1)} ${units[unit]}`;
}

const TYPE_LABEL: Record<Backup["type"], string> = {
  manual: "Manual",
  scheduled: "Scheduled",
  update: "Update",
};

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; supported: boolean; backups: Backup[] };

/**
 * The System → Backups view (COL-63): lists the database backups on disk and
 * drives the manual "Backup now" action (`POST /api/system/backup`).
 *
 * When the database isn't file-based SQLite the server reports
 * `supported: false`; the page then shows an "unavailable for this database
 * configuration" state instead of the controls. Timestamps render in local
 * time.
 */
export function BackupsPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function load() {
    try {
      const list = await fetchBackups();
      setState({ status: "ready", supported: list.supported, backups: list.backups });
    } catch (error: unknown) {
      setState({
        status: "error",
        message: error instanceof Error ? error.message : "Unknown error.",
      });
    }
  }

  useEffect(() => {
    let cancelled = false;
    fetchBackups()
      .then((list) => {
        if (!cancelled) {
          setState({ status: "ready", supported: list.supported, backups: list.backups });
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

  async function handleBackupNow() {
    setCreating(true);
    setActionError(null);
    try {
      await createBackup();
      await load();
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Failed to create backup.");
    } finally {
      setCreating(false);
    }
  }

  const supported = state.status === "ready" ? state.supported : true;

  return (
    <section className="view">
      <header className="view__header view__header--row">
        <div>
          <h1 className="view__title">Backups</h1>
          <p className="view__summary">
            Snapshots of the Collapsarr database. Take one now, or let scheduled backups run.
          </p>
        </div>
        {state.status === "ready" && supported && (
          <div className="view__actions">
            <button
              type="button"
              className="btn btn--primary"
              onClick={handleBackupNow}
              disabled={creating}
            >
              {creating ? "Backing up…" : "Backup now"}
            </button>
          </div>
        )}
      </header>

      {actionError && <p className="view__error">{actionError}</p>}

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading backups…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <BackupIcon width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load backups: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && !supported && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <BackupIcon width={28} height={28} />
          </span>
          <p className="panel__message">
            Backups are unavailable for this database configuration. Snapshots are only
            supported for a file-based SQLite database.
          </p>
        </div>
      )}

      {state.status === "ready" && supported && state.backups.length === 0 && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <BackupIcon width={28} height={28} />
          </span>
          <p className="panel__message">
            No backups yet. Use <strong>Backup now</strong> to create the first one.
          </p>
        </div>
      )}

      {state.status === "ready" && supported && state.backups.length > 0 && (
        <div className="panel">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Type</th>
                <th scope="col">Size</th>
                <th scope="col">Created</th>
              </tr>
            </thead>
            <tbody>
              {state.backups.map((backup) => (
                <tr key={backup.id}>
                  <td>{backup.name}</td>
                  <td>{TYPE_LABEL[backup.type] ?? backup.type}</td>
                  <td>{formatSize(backup.size)}</td>
                  <td>{formatTimestamp(backup.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
