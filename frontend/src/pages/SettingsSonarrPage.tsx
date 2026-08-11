import { InstancesSection } from "../components/settings/InstancesSection";

/**
 * Settings → Sonarr (COL-144): one of the two type-scoped instance pages that
 * replace the old composed `SettingsPage`'s combined Sonarr/Radarr table.
 * Renders `InstancesSection` fixed to `type: "sonarr"`, so it only lists
 * Sonarr instances and its create form has no type selector -- `Radarr`'s
 * sibling page (`SettingsRadarrPage`) renders the same component fixed to
 * `"radarr"` instead, rather than duplicating the CRUD logic.
 *
 * Keeps the same `view__header`/`h1.view__title` shell every other Settings
 * sub-nav page has (`SettingsGeneralPage`, COL-142; `SettingsTargetsPage`/
 * `SettingsConnectPage`, COL-143).
 */
export function SettingsSonarrPage() {
  return (
    <div className="view view--settings">
      <header className="view__header">
        <h1 className="view__title">Sonarr</h1>
        <p className="view__summary">Sonarr connections and their remote-to-local path mappings.</p>
      </header>

      <div className="settings-sections">
        <InstancesSection type="sonarr" />
      </div>
    </div>
  );
}
