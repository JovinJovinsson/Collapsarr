import { useEffect, useState } from "react";

import { fetchSettings, updateSettings } from "../../api/settings";
import { LANGUAGE_OPTIONS } from "../../types/languages";
import { DOWNMIX_TARGET_OPTIONS } from "../../types/wanted";
import type { DownmixTarget } from "../../types/wanted";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready" };

/** Default `default_audio_delay_minutes` (COL-243), matching the backend's documented default. */
const DEFAULT_AUDIO_DELAY_MINUTES = "30";

/**
 * Preferred Default Audio (COL-159): a language + channel-tier pair used to
 * pick which resulting downmix track becomes a file's default audio track,
 * plus the "automatically set" opt-in gating whether the downmix pipeline
 * actually applies it. Backed by COL-151's `GET`/`PUT /api/settings` fields
 * (`default_audio_language`/`default_audio_channel_tier`/
 * `auto_set_default_audio`).
 *
 * Also owns `default_audio_delay_minutes` (COL-243): the minimum age (in
 * minutes) a remuxed file's new audio streams must have before Collapsarr
 * trusts Plex to have already processed them for a Default Audio Track
 * write/verify. Grouped here rather than in `GeneralSection` since it's
 * specific to this feature; not yet consumed by any Job-scheduling logic --
 * COL-251 is the follow-up ticket that will read it.
 */
export function DefaultAudioSection() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [language, setLanguage] = useState("");
  const [channelTier, setChannelTier] = useState<DownmixTarget | "">("");
  const [autoSet, setAutoSet] = useState(false);
  const [delayMinutes, setDelayMinutes] = useState(DEFAULT_AUDIO_DELAY_MINUTES);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    fetchSettings()
      .then((settings) => {
        setLanguage(settings.default_audio_language ?? "");
        setChannelTier(settings.default_audio_channel_tier ?? "");
        setAutoSet(settings.auto_set_default_audio);
        setDelayMinutes(String(settings.default_audio_delay_minutes));
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
    setError(null);
    const delay = Number(delayMinutes);
    if (delayMinutes.trim() === "" || !Number.isInteger(delay) || delay < 0) {
      setError("Default audio delay must be a whole number of 0 or more.");
      return;
    }
    setSaving(true);
    setSavedAt(null);
    try {
      const trimmedLanguage = language.trim();
      const updated = await updateSettings({
        default_audio_language: trimmedLanguage.length > 0 ? trimmedLanguage : null,
        default_audio_channel_tier: channelTier === "" ? null : channelTier,
        // Belt-and-braces: never persist the toggle on without both fields
        // set, even if state somehow got out of sync with the UI guard above.
        auto_set_default_audio: bothSet ? autoSet : false,
        default_audio_delay_minutes: delay,
      });
      setLanguage(updated.default_audio_language ?? "");
      setChannelTier(updated.default_audio_channel_tier ?? "");
      setAutoSet(updated.auto_set_default_audio);
      setDelayMinutes(String(updated.default_audio_delay_minutes));
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
              {/*
                A value saved before this ticket (free text) or written
                directly via the API can be a code outside LANGUAGE_OPTIONS.
                Render it as an extra option rather than silently falling
                back to "None" while the real value is still saved.
              */}
              {language !== "" && !LANGUAGE_OPTIONS.some((option) => option.value === language) && (
                <option value={language}>{language} (not in list)</option>
              )}
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

          <div className="form-field form-field--narrow">
            <label htmlFor="default-audio-delay-minutes">Default audio delay (minutes)</label>
            <input
              id="default-audio-delay-minutes"
              type="number"
              min={0}
              value={delayMinutes}
              onChange={(event) => setDelayMinutes(event.target.value)}
            />
            <p className="form-hint">
              Durations shorter than 30 minutes may fail if your Plex has not processed the new
              audio streams in the media file yet.
            </p>
          </div>

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
