import { useEffect, useState } from "react";

import { fetchSettings, updateSettings } from "../../api/settings";
import { LANGUAGE_OPTIONS } from "../../types/languages";
import { DOWNMIX_TARGET_OPTIONS } from "../../types/wanted";
import type { DownmixTarget } from "../../types/wanted";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready" };

/**
 * Preferred Default Audio (COL-159): a language + channel-tier pair used to
 * pick which resulting downmix track becomes a file's default audio track,
 * plus the "automatically set" opt-in gating whether the downmix pipeline
 * actually applies it. Backed by COL-151's `GET`/`PUT /api/settings` fields
 * (`default_audio_language`/`default_audio_channel_tier`/
 * `auto_set_default_audio`).
 */
export function DefaultAudioSection() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [language, setLanguage] = useState("");
  const [channelTier, setChannelTier] = useState<DownmixTarget | "">("");
  const [autoSet, setAutoSet] = useState(false);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    fetchSettings()
      .then((settings) => {
        setLanguage(settings.default_audio_language ?? "");
        setChannelTier(settings.default_audio_channel_tier ?? "");
        setAutoSet(settings.auto_set_default_audio);
        setState({ status: "ready" });
      })
      .catch((err: unknown) =>
        setState({ status: "error", message: err instanceof Error ? err.message : "Unknown error." }),
      );
  }, []);

  const bothSet = language.trim().length > 0 && channelTier !== "";

  // Clearing either field after the checkbox was on would otherwise leave a
  // checked-but-disabled checkbox and an invalid combination queued for the
  // next save -- clear the checkbox state itself the moment either field
  // empties.
  function handleLanguageChange(value: string) {
    setLanguage(value);
    if (value.trim().length === 0) {
      setAutoSet(false);
    }
  }

  function handleChannelTierChange(value: string) {
    const next = value as DownmixTarget | "";
    setChannelTier(next);
    if (next === "") {
      setAutoSet(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    setSavedAt(null);
    try {
      const trimmedLanguage = language.trim();
      const updated = await updateSettings({
        default_audio_language: trimmedLanguage.length > 0 ? trimmedLanguage : null,
        default_audio_channel_tier: channelTier === "" ? null : channelTier,
        // Belt-and-braces: never persist the toggle on without both fields
        // set, even if state somehow got out of sync with the UI guard above.
        auto_set_default_audio: bothSet ? autoSet : false,
      });
      setLanguage(updated.default_audio_language ?? "");
      setChannelTier(updated.default_audio_channel_tier ?? "");
      setAutoSet(updated.auto_set_default_audio);
      setSavedAt(Date.now());
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Unknown error.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="settings-section">
      <div className="settings-section__header">
        <div>
          <h2 className="settings-section__title">Preferred default audio</h2>
          <p className="settings-section__summary">
            Which audio language and channel tier to set as a file&apos;s default audio track after downmixing.
          </p>
        </div>
      </div>

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading preferred default audio…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <p className="panel__message">Couldn&apos;t load settings: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && (
        <div className="panel settings-form">
          <div className="form-field form-field--narrow">
            <label htmlFor="default-audio-language">Preferred language</label>
            <select
              id="default-audio-language"
              value={language}
              onChange={(event) => handleLanguageChange(event.target.value)}
            >
              <option value="">None</option>
              {LANGUAGE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <div className="form-field form-field--narrow">
            <label htmlFor="default-audio-channel-tier">Preferred channel tier</label>
            <select
              id="default-audio-channel-tier"
              value={channelTier}
              onChange={(event) => handleChannelTierChange(event.target.value)}
            >
              <option value="">None</option>
              {DOWNMIX_TARGET_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={autoSet}
              disabled={!bothSet}
              onChange={(event) => setAutoSet(event.target.checked)}
            />
            Automatically set the default audio track when downmixing
          </label>

          <div className="form-actions">
            <button type="button" className="btn btn--primary" onClick={handleSave} disabled={saving}>
              {saving ? "Saving…" : "Save preferred default audio"}
            </button>
            {savedAt !== null && !error && <span className="form-success">Saved.</span>}
          </div>
          {error && <p className="form-error">{error}</p>}
        </div>
      )}
    </section>
  );
}
