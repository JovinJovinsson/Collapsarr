import { InstancesSection } from "../components/settings/InstancesSection";

/**
 * The old composed Settings view (COL-33, plus Connect from COL-36):
 * instances + path mappings, downmix targets + language allow-list, and
 * Connect (webhook + Discord notifiers). Split into independently-fetching
 * sections, each owning its own fetch/save cycle against `GET`/`PUT
 * /api/settings` (COL-28), `/api/instances` (COL-27), and `/api/notifiers`
 * (COL-36).
 *
 * General moved to its own dedicated page at `/settings/general`
 * (`SettingsGeneralPage`, COL-142); Targets and Connect moved to
 * `/settings/targets`/`/settings/connect` (`SettingsTargetsPage`/
 * `SettingsConnectPage`, COL-143) -- the second and third of Settings'
 * Bazarr-style sub-nav pages. This page keeps serving its one remaining
 * section (Instances) at the old `/settings` route until it migrates too
 * (later ticket in this Epic, COL-144); no redirect off this route is wired
 * yet (that ships with that last migration).
 */
export function SettingsPage() {
  return (
    <div className="view view--settings">
      <header className="view__header">
        <h1 className="view__title">Settings</h1>
        <p className="view__summary">Arr instances and path mappings.</p>
      </header>

      <div className="settings-sections">
        <InstancesSection />
      </div>
    </div>
  );
}
