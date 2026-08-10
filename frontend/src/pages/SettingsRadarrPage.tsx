import { InstancesSection } from "../components/settings/InstancesSection";

/**
 * Settings → Radarr (COL-144): the sibling of `SettingsSonarrPage`, one of
 * the two type-scoped instance pages that replace the old composed
 * `SettingsPage`'s combined Sonarr/Radarr table. Renders `InstancesSection`
 * fixed to `type: "radarr"`, so it only lists Radarr instances and its
 * create form has no type selector.
 *
 * Keeps the same `view__header`/`h1.view__title` shell every other Settings
 * sub-nav page has (`SettingsGeneralPage`, COL-142; `SettingsTargetsPage`/
 * `SettingsConnectPage`, COL-143).
 */
export function SettingsRadarrPage() {
  return (
    <div className="view view--settings">
      <header className="view__header">
        <h1 className="view__title">Radarr</h1>
        <p className="view__summary">Radarr connections and their remote-to-local path mappings.</p>
      </header>

      <div className="settings-sections">
        <InstancesSection type="radarr" />
      </div>
    </div>
  );
}
