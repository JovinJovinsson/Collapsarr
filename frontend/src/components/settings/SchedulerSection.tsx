import { useEffect, useState } from "react";

import { fetchSettings, updateSettings } from "../../api/settings";
import { fetchTasks } from "../../api/tasks";
import type { ScheduledTask } from "../../types/tasks";

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

type TasksLoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; tasks: ScheduledTask[] };

/** The Scheduled Task registry's row name for the editable panel above -- excluded from the read-only list. */
const BACKUPS_TASK_NAME = "Backups";

/**
 * Settings → Scheduler (COL-146): the editable "Backup schedule" panel --
 * interval/retention days, backed by `GET`/`PUT /api/settings` -- moved
 * verbatim off `BackupsPage` (where COL-66 originally added it) onto its own
 * Settings sub-nav page. `BackupsPage` keeps its backup list and actions
 * (Backup now, Download, Delete, Restore, Upload & Restore) unchanged; only
 * the schedule panel relocated.
 *
 * Below it, a read-only section lists the current cadence of the other three
 * periodic tasks -- Library Scan, Health Checks, Update Check -- sourced from
 * the existing Scheduled Task registry (`GET /api/system/tasks`,
 * `fetchTasks()`, COL-122). The "Backups" row is excluded here since it has
 * its own editable panel above. These three cadences aren't configurable
 * yet -- this section is informational only, no new backend work.
 */
export function SchedulerSection() {
  const [scheduleState, setScheduleState] = useState<ScheduleLoadState>({ status: "loading" });
  const [scheduleForm, setScheduleForm] = useState<ScheduleFormValues>({
    intervalDays: "7",
    retentionDays: "28",
  });
  const [scheduleSaving, setScheduleSaving] = useState(false);
  const [scheduleError, setScheduleError] = useState<string | null>(null);
  const [scheduleSavedAt, setScheduleSavedAt] = useState<number | null>(null);

  const [tasksState, setTasksState] = useState<TasksLoadState>({ status: "loading" });

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

  useEffect(() => {
    let cancelled = false;
    fetchTasks()
      .then((tasks) => {
        if (!cancelled) {
          setTasksState({ status: "ready", tasks });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setTasksState({
            status: "error",
            message: error instanceof Error ? error.message : "Unknown error.",
          });
        }
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

  const otherTasks =
    tasksState.status === "ready" ? tasksState.tasks.filter((task) => task.name !== BACKUPS_TASK_NAME) : [];

  return (
    <section className="settings-section">
      <div className="settings-section__header">
        <div>
          <h2 className="settings-section__title">Scheduler</h2>
          <p className="settings-section__summary">
            How often backups run and are kept, plus the cadence of the other background tasks.
          </p>
        </div>
      </div>

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

      <div className="panel settings-form">
        <h3 className="settings-form__subtitle">Other scheduled tasks</h3>
        <p className="form-hint">
          Library scan, health checks, and the update check each run on a fixed cadence. They
          aren&apos;t configurable yet.
        </p>

        {tasksState.status === "loading" && <p className="panel__message">Loading task cadence…</p>}

        {tasksState.status === "error" && (
          <p className="form-error">Couldn&apos;t load task cadence: {tasksState.message}</p>
        )}

        {tasksState.status === "ready" && (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Interval</th>
              </tr>
            </thead>
            <tbody>
              {otherTasks.map((task) => (
                <tr key={task.name}>
                  <td>{task.name}</td>
                  <td>{task.interval_label}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
