import { ConnectSection } from "../components/settings/ConnectSection";
import { InstancesSection } from "../components/settings/InstancesSection";
import { TargetsSection } from "../components/settings/TargetsSection";

/**
 * The old composed Settings view (COL-33, plus Connect from COL-36):
 * instances + path mappings, downmix targets + language allow-list, and
 * Connect (webhook + Discord notifiers). Split into independently-fetching
 * sections, each owning its own fetch/save cycle against `GET`/`PUT
 * /api/settings` (COL-28), `/api/instances` (COL-27), and `/api/notifiers`
 * (COL-36).
 *
 * General moved to its own dedicated page at `/settings/general`
 * (`SettingsGeneralPage`, COL-142) -- the first of Settings' Bazarr-style
 * sub-nav pages. This page keeps serving its remaining sections
 * (Instances/Targets/Connect) at the old `/settings` route until each has
 * migrated too (later tickets in this Epic); no redirect off this route is
 * wired yet (that ships with the last migration, COL-144).
 */
export function SettingsPage() {
  return (
    <div className="view view--settings">
      <header className="view__header">
        <h1 className="view__title">Settings</h1>
        <p className="view__summary">Instances, downmix targets, and Connect.</p>
      </header>

      <div className="settings-sections">
        <InstancesSection />
        <TargetsSection />
        <ConnectSection />
      </div>
    </div>
  );
}
