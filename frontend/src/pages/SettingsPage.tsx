import { ConnectSection } from "../components/settings/ConnectSection";
import { GeneralSection } from "../components/settings/GeneralSection";
import { InstancesSection } from "../components/settings/InstancesSection";
import { TargetsSection } from "../components/settings/TargetsSection";

/**
 * The Settings view (COL-33, plus Connect from COL-36): instances + path
 * mappings, downmix targets + language allow-list, Connect (webhook +
 * Discord notifiers), and general options (API key, auth, concurrency,
 * codec/bitrate overrides). Split into independently-fetching sections, each
 * owning its own fetch/save cycle against `GET`/`PUT /api/settings` (COL-28),
 * `/api/instances` (COL-27), and `/api/notifiers` (COL-36).
 */
export function SettingsPage() {
  return (
    <div className="view view--settings">
      <header className="view__header">
        <h1 className="view__title">Settings</h1>
        <p className="view__summary">Instances, downmix targets, Connect, and general options.</p>
      </header>

      <div className="settings-sections">
        <InstancesSection />
        <TargetsSection />
        <ConnectSection />
        <GeneralSection />
      </div>
    </div>
  );
}
