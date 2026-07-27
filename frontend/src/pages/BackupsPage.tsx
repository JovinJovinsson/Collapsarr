import { useEffect, useState } from "react";

import { createBackup, deleteBackup, downloadBackup, fetchBackups, restoreBackup } from "../api/backups";
import { fetchSettings, updateSettings } from "../api/settings";
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
  restore: "Restore",
};

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; supported: boolean; backups: Backup[] };

type ScheduleLoadState = { status: "loading" } | { status: "error"; message: string } | { status: "ready" };

interface ScheduleFormValues {
  intervalDays: string;
  retentionDays: string;
}

/** Validates the backup-schedule form; returns an error message, or `null` when valid. */
function validateScheduleForm(form: ScheduleFormValues): string | null {
  const interval = Number(form.intervalDays);
  if (form.intervalDays.trim() === "" || !Number.isInteger(interval) || interval < 1) {
    return "Backup interval must be a whole number of 1 or more.";
  }
  const retention = Number(form.retentionDays);
  if (form.retentionDays.trim() === "" || !Number.isInteger(retention) || retention < 1) {
    return "Backup retention must be a whole number of 1 or more.";
  }
  return null;
}

/**
 * The System → Backups view (COL-63): lists the database backups on disk and
 * drives the manual "Backup now" action (`POST /api/system/backup`).
 *
 * When the database isn't file-based SQLite the server reports
 * `supported: false`; the page then shows an "unavailable for this database
 * configuration" state instead of the controls. Timestamps render in local
 * time.
 *
 * COL-66 adds the inline "Backup schedule" panel: interval/retention days,
 * backed by the same `GET`/`PUT /api/settings` the Settings page's
 * `GeneralSection` uses (Radarr-style -- no dedicated backup-settings
 * endpoint). The values are inert here -- COL-67's scheduler reads the
 * interval and COL-68's pruning reads the retention; this page only persists
 * the knobs.
 *
 * COL-64 adds a per-row "Download" action, streaming the archive off disk via
 * `GET /api/system/backup/{id}/download` (`downloadBackup`, `api/backups.ts`).
 *
 * COL-65 adds a per-row "Delete" action with an inline confirmation step
 * (Confirm delete / Cancel), calling `DELETE /api/system/backup/{id}`
 * (`deleteBackup`). The server enforces a minimum-keep floor: a delete that
 * would remove the last recovery point is refused with a `409`, whose message
 * is surfaced in the same `actionError` banner as the other row actions.
 *
 * COL-71 adds a per-row "Restore" action, same inline-confirm shape as
 * Delete, calling `POST /api/system/backup/restore/{id}` (`restoreBackup`).
 * It is destructive in a different way than Delete -- it doesn't lose data,
 * but it logs out every active session and can change the API key (both
 * revert to the backup's values), and it restarts the app -- so the confirm
 * step spells that out explicitly rather than reusing Delete's generic
 * wording. A gate rejection (bad archive / not SQLite / missing sentinel
 * table, `422`) or unknown id (`404`) surfaces in `actionError`, same as the
 * other actions; a successful restore instead shows a persistent `view__notice`
 * banner (the app is restarting, so refreshing the list would just fail).
 */
export function BackupsPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [confirmingRestoreId, setConfirmingRestoreId] = useState<string | null>(null);
  const [restoringId, setRestoringId] = useState<string | null>(null);
  const [restoreStarted, setRestoreStarted] = useState(false);

  const [scheduleState, setScheduleState] = useState<ScheduleLoadState>({ status: "loading" });
  const [scheduleForm, setScheduleForm] = useState<ScheduleFormValues>({
    intervalDays: "7",
    retentionDays: "28",
  });
  const [scheduleSaving, setScheduleSaving] = useState(false);
  const [scheduleError, setScheduleError] = useState<string | null>(null);
  const [scheduleSavedAt, setScheduleSavedAt] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchSettings()
      .then((settings) => {
        if (cancelled) return;
        setScheduleForm({
          intervalDays: String(settings.backup_interval_days),
          retentionDays: String(settings.backup_retention_days),
        });
        setScheduleState({ status: "ready" });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setScheduleState({
          status: "error",
          message: error instanceof Error ? error.message : "Unknown error.",
        });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSaveSchedule() {
    const validationError = validateScheduleForm(scheduleForm);
    if (validationError) {
      setScheduleError(validationError);
      return;
    }
    setScheduleSaving(true);
    setScheduleError(null);
    setScheduleSavedAt(null);
    try {
      const updated = await updateSettings({
        backup_interval_days: Number(scheduleForm.intervalDays),
        backup_retention_days: Number(scheduleForm.retentionDays),
      });
      setScheduleForm({
        intervalDays: String(updated.backup_interval_days),
        retentionDays: String(updated.backup_retention_days),
      });
      setScheduleSavedAt(Date.now());
    } catch (error: unknown) {
      setScheduleError(error instanceof Error ? error.message : "Failed to save the backup schedule.");
    } finally {
      setScheduleSaving(false);
    }
  }

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

  async function handleDownload(backup: Backup) {
    setDownloadingId(backup.id);
    setActionError(null);
    try {
      await downloadBackup(backup);
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Failed to download backup.");
    } finally {
      setDownloadingId(null);
    }
  }

  async function handleConfirmDelete(backup: Backup) {
    setDeletingId(backup.id);
    setActionError(null);
    try {
      await deleteBackup(backup);
      setConfirmingDeleteId(null);
      await load();
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Failed to delete backup.");
    } finally {
      setDeletingId(null);
    }
  }

  async function handleConfirmRestore(backup: Backup) {
    setRestoringId(backup.id);
    setActionError(null);
    try {
      await restoreBackup(backup);
      setConfirmingRestoreId(null);
      // The server has already staged the restore and is shutting down --
      // don't re-fetch the list (the app may already be on its way down);
      // show a persistent notice instead.
      setRestoreStarted(true);
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Failed to restore backup.");
    } finally {
      setRestoringId(null);
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

      {restoreStarted && (
        <p className="view__notice" role="status">
          Restore started. Collapsarr is restarting to apply it — reload this page in a few
          seconds. If this install isn&apos;t running under a supervisor (Docker or systemd) that
          restarts it automatically, you&apos;ll need to start it again manually.
        </p>
      )}

      <div className="panel settings-form">
        <h3 className="settings-form__subtitle">Backup schedule</h3>

        {scheduleState.status === "loading" && (
          <p className="panel__message">Loading schedule…</p>
        )}

        {scheduleState.status === "error" && (
          <p className="form-error">Couldn&apos;t load the backup schedule: {scheduleState.message}</p>
        )}

        {scheduleState.status === "ready" && (
          <>
            <div className="form-grid">
              <div className="form-field form-field--narrow">
                <label htmlFor="backup-interval-days">Backup interval (days)</label>
                <input
                  id="backup-interval-days"
                  type="number"
                  min={1}
                  value={scheduleForm.intervalDays}
                  onChange={(event) =>
                    setScheduleForm({ ...scheduleForm, intervalDays: event.target.value })
                  }
                />
                <p className="form-hint">How often a scheduled backup runs.</p>
              </div>
              <div className="form-field form-field--narrow">
                <label htmlFor="backup-retention-days">Backup retention (days)</label>
                <input
                  id="backup-retention-days"
                  type="number"
                  min={1}
                  value={scheduleForm.retentionDays}
                  onChange={(event) =>
                    setScheduleForm({ ...scheduleForm, retentionDays: event.target.value })
                  }
                />
                <p className="form-hint">How long a backup is kept before it&apos;s pruned.</p>
              </div>
            </div>
            <div className="form-actions">
              <button
                type="button"
                className="btn btn--primary"
                onClick={handleSaveSchedule}
                disabled={scheduleSaving}
              >
                {scheduleSaving ? "Saving…" : "Save schedule"}
              </button>
              {scheduleSavedAt !== null && !scheduleError && <span className="form-success">Saved.</span>}
            </div>
            {scheduleError && <p className="form-error">{scheduleError}</p>}
          </>
        )}
      </div>

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
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {state.backups.map((backup) => (
                <tr key={backup.id}>
                  <td>{backup.name}</td>
                  <td>{TYPE_LABEL[backup.type] ?? backup.type}</td>
                  <td>{formatSize(backup.size)}</td>
                  <td>{formatTimestamp(backup.created_at)}</td>
                  <td className="data-table__actions">
                    <button
                      type="button"
                      className="btn btn--secondary btn--sm"
                      onClick={() => handleDownload(backup)}
                      disabled={downloadingId === backup.id}
                    >
                      {downloadingId === backup.id ? "Downloading…" : "Download"}
                    </button>
                    {confirmingRestoreId === backup.id ? (
                      <>
                        <span className="data-table__confirm" role="status">
                          Restoring this backup will log out active sessions and may change the
                          API key (both revert to the backup&apos;s values). Collapsarr will
                          restart to apply it — if this install isn&apos;t supervised
                          (Docker/systemd), restart it yourself afterwards.
                        </span>
                        <button
                          type="button"
                          className="btn btn--danger btn--sm"
                          onClick={() => handleConfirmRestore(backup)}
                          disabled={restoringId === backup.id}
                        >
                          {restoringId === backup.id ? "Restoring…" : "Confirm restore"}
                        </button>
                        <button
                          type="button"
                          className="btn btn--ghost btn--sm"
                          onClick={() => setConfirmingRestoreId(null)}
                          disabled={restoringId === backup.id}
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        className="btn btn--secondary btn--sm"
                        onClick={() => setConfirmingRestoreId(backup.id)}
                        disabled={restoreStarted}
                      >
                        Restore
                      </button>
                    )}
                    {confirmingDeleteId === backup.id ? (
                      <>
                        <span className="data-table__confirm" role="status">
                          Delete this backup?
                        </span>
                        <button
                          type="button"
                          className="btn btn--danger btn--sm"
                          onClick={() => handleConfirmDelete(backup)}
                          disabled={deletingId === backup.id}
                        >
                          {deletingId === backup.id ? "Deleting…" : "Confirm delete"}
                        </button>
                        <button
                          type="button"
                          className="btn btn--ghost btn--sm"
                          onClick={() => setConfirmingDeleteId(null)}
                          disabled={deletingId === backup.id}
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        className="btn btn--danger btn--sm"
                        onClick={() => setConfirmingDeleteId(backup.id)}
                      >
                        Delete
                      </button>
                    )}
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
