import { PlexSection } from "../components/settings/PlexSection";

/**
 * Settings → Plex (COL-209): the singleton Plex Connection page, structurally
 * consistent with the other Settings sub-nav pages (`SettingsSonarrPage`,
 * `SettingsConnectPage`) but wrapping `PlexSection` -- which backs one config
 * row rather than a CRUD instance list, since Collapsarr talks to exactly one
 * Plex Media Server.
 *
 * Keeps the same `view__header`/`h1.view__title` shell every other Settings
 * sub-nav page has.
 */
export function SettingsPlexPage() {
  return (
    <div className="view view--settings">
      <header className="view__header">
        <h1 className="view__title">Plex</h1>
        <p className="view__summary">
          Connect to a Plex Media Server. The token is stored server-side only.
        </p>
      </header>

      <div className="settings-sections">
        <PlexSection />
      </div>
    </div>
  );
}
