import { LoggingSection } from "../components/settings/LoggingSection";

/**
 * Settings → Logging (COL-147): the seventh and last of Settings'
 * Bazarr-style sub-nav pages. Wraps `LoggingSection` unchanged -- the
 * `log_level` runtime-override control moved here verbatim from
 * `GeneralSection` (where COL-130 originally added it, and where
 * `SettingsGeneralPage`'s COL-142 doc comment expected it to move in
 * "Phase 2").
 *
 * Keeps the same `view__header`/`h1.view__title` shell `SettingsGeneralPage`
 * (COL-142) established for Settings' sub-nav pages.
 */
export function SettingsLoggingPage() {
  return (
    <div className="view view--settings">
      <header className="view__header">
        <h1 className="view__title">Logging</h1>
        <p className="view__summary">The runtime log-level override.</p>
      </header>

      <div className="settings-sections">
        <LoggingSection />
      </div>
    </div>
  );
}
