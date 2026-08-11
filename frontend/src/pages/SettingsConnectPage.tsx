import { ConnectSection } from "../components/settings/ConnectSection";

/**
 * Settings → Connect (COL-143): the third of Settings' Bazarr-style sub-nav
 * pages, split out of the old composed `SettingsPage`. Wraps `ConnectSection`
 * unchanged -- the generic webhook and Discord notifier config.
 *
 * Keeps the same `view__header`/`h1.view__title` shell `SettingsGeneralPage`
 * (COL-142) established for Settings' sub-nav pages.
 */
export function SettingsConnectPage() {
  return (
    <div className="view view--settings">
      <header className="view__header">
        <h1 className="view__title">Connect</h1>
        <p className="view__summary">
          Notify a generic webhook and/or Discord on downmix failure and app health issues.
        </p>
      </header>

      <div className="settings-sections">
        <ConnectSection />
      </div>
    </div>
  );
}
