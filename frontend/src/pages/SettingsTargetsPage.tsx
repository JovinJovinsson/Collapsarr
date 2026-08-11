import { DefaultAudioSection } from "../components/settings/DefaultAudioSection";
import { TargetsSection } from "../components/settings/TargetsSection";

/**
 * Settings → Targets (COL-143): the second of Settings' Bazarr-style sub-nav
 * pages, split out of the old composed `SettingsPage`. Wraps `TargetsSection`
 * unchanged -- downmix targets (Stereo/2.1/5.1) and the language allow-list --
 * plus `DefaultAudioSection` (COL-159): Preferred Default Audio language +
 * channel-tier pickers and the "automatically set" opt-in.
 *
 * Keeps the same `view__header`/`h1.view__title` shell `SettingsGeneralPage`
 * (COL-142) established for Settings' sub-nav pages.
 */
export function SettingsTargetsPage() {
  return (
    <div className="view view--settings">
      <header className="view__header">
        <h1 className="view__title">Targets</h1>
        <p className="view__summary">Which downmix targets to produce, and which languages to consider.</p>
      </header>

      <div className="settings-sections">
        <TargetsSection />
        <DefaultAudioSection />
      </div>
    </div>
  );
}
