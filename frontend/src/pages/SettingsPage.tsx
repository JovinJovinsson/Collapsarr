import { GeneralSection } from "../components/settings/GeneralSection";
import { InstancesSection } from "../components/settings/InstancesSection";
import { TargetsSection } from "../components/settings/TargetsSection";

/**
 * The Settings view (COL-33): instances + path mappings, downmix targets +
 * language allow-list, and general options (API key, auth, concurrency,
 * codec/bitrate overrides). Split into three independent sections, each
 * owning its own fetch/save cycle against `GET`/`PUT /api/settings` (COL-28)
 * and `/api/instances` (COL-27) -- Connect/notifications settings are out of
 * scope until their config fields exist (separate epic).
 */
export function SettingsPage() {
  return (
    <div className="view view--settings">
      <header className="view__header">
        <h1 className="view__title">Settings</h1>
        <p className="view__summary">Instances, downmix targets, and general options.</p>
      </header>

      <div className="settings-sections">
        <InstancesSection />
        <TargetsSection />
        <GeneralSection />
      </div>
    </div>
  );
}
