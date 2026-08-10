import { GeneralSection } from "../components/settings/GeneralSection";

/**
 * Settings → General (COL-142): the first of Settings' Bazarr-style sub-nav
 * pages, split out of the old composed `SettingsPage`. Wraps `GeneralSection`
 * unchanged -- API key, auth, concurrency, codec/bitrate overrides,
 * disk-space alerts, credential change, log-out-everywhere. `log_level`
 * moved to its own Logging page in COL-147 (Phase 2).
 *
 * Keeps the same `view__header`/`h1.view__title` shell every other
 * AppShell-routed page has (e.g. `TasksPage`, `HealthChecksPage`) even
 * though `GeneralSection` also renders its own `<h2>` -- that's the same
 * page-h1-plus-section-h2/h3 nesting `GeneralSection` already had under
 * `SettingsPage`'s `<h1>Settings</h1>` before this split, not a new
 * duplication.
 */
export function SettingsGeneralPage() {
  return (
    <div className="view view--settings">
      <header className="view__header">
        <h1 className="view__title">General</h1>
        <p className="view__summary">
          API key, authentication, concurrency, codec/bitrate overrides, and disk-space alerts.
        </p>
      </header>

      <div className="settings-sections">
        <GeneralSection />
      </div>
    </div>
  );
}
