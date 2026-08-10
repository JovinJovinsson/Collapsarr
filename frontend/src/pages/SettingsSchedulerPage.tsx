import { SchedulerSection } from "../components/settings/SchedulerSection";

/**
 * Settings → Scheduler (COL-146): the sixth of Settings' Bazarr-style sub-nav
 * pages. Wraps `SchedulerSection` unchanged -- the editable backup
 * interval/retention panel (moved here from `BackupsPage`, where COL-66
 * originally added it) plus a read-only cadence readout for the other three
 * periodic tasks (Library Scan, Health Checks, Update Check).
 *
 * Keeps the same `view__header`/`h1.view__title` shell `SettingsGeneralPage`
 * (COL-142) established for Settings' sub-nav pages.
 */
export function SettingsSchedulerPage() {
  return (
    <div className="view view--settings">
      <header className="view__header">
        <h1 className="view__title">Scheduler</h1>
        <p className="view__summary">
          The backup schedule, plus the current cadence of the other periodic tasks.
        </p>
      </header>

      <div className="settings-sections">
        <SchedulerSection />
      </div>
    </div>
  );
}
