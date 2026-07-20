import { useEffect, useState } from "react";

import { getStoredApiKey, setStoredApiKey } from "../../api/client";
import { fetchSettings, updateSettings } from "../../api/settings";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready" };

interface GeneralFormValues {
  concurrencyLimit: string;
  uiAuthEnabled: boolean;
  stereoCodec: string;
  stereoBitrateKbps: string;
  surroundCodec: string;
  surroundBitrateKbps: string;
}

/** Validates the general-settings form; returns an error message, or `null` when valid. */
function validateGeneralForm(form: GeneralFormValues): string | null {
  const concurrency = Number(form.concurrencyLimit);
  if (form.concurrencyLimit.trim() === "" || !Number.isInteger(concurrency) || concurrency < 1) {
    return "Concurrency limit must be a whole number of 1 or more.";
  }
  if (!form.stereoCodec.trim()) return "Stereo codec is required.";
  if (!form.surroundCodec.trim()) return "Surround codec is required.";
  if (form.stereoBitrateKbps.trim() !== "") {
    const value = Number(form.stereoBitrateKbps);
    if (!Number.isInteger(value) || value < 1) return "Stereo bitrate must be a positive whole number, or blank.";
  }
  if (form.surroundBitrateKbps.trim() !== "") {
    const value = Number(form.surroundBitrateKbps);
    if (!Number.isInteger(value) || value < 1) return "Surround bitrate must be a positive whole number, or blank.";
  }
  return null;
}

/**
 * General settings (COL-33's AC3): API key display, auth toggle,
 * concurrency, and codec/bitrate advanced overrides, backed by COL-28's
 * `GET`/`PUT /api/settings`.
 *
 * Also owns the browser-local API key used by `apiFetch` (`client.ts`,
 * COL-33's auth retrofit) -- the server's auto-generated key is displayed
 * read-only here, and the user copies it into (or edits) the "this
 * browser's key" field to authenticate outgoing requests.
 */
export function GeneralSection() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [serverApiKey, setServerApiKey] = useState("");
  const [form, setForm] = useState<GeneralFormValues>({
    concurrencyLimit: "1",
    uiAuthEnabled: false,
    stereoCodec: "aac",
    stereoBitrateKbps: "",
    surroundCodec: "ac3",
    surroundBitrateKbps: "",
  });

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const [browserApiKey, setBrowserApiKey] = useState(() => getStoredApiKey());
  const [browserKeySavedAt, setBrowserKeySavedAt] = useState<number | null>(null);

  useEffect(() => {
    fetchSettings()
      .then((settings) => {
        setServerApiKey(settings.api_key);
        setForm({
          concurrencyLimit: String(settings.concurrency_limit),
          uiAuthEnabled: settings.ui_auth_enabled,
          stereoCodec: settings.stereo_codec,
          stereoBitrateKbps: settings.stereo_bitrate_kbps === null ? "" : String(settings.stereo_bitrate_kbps),
          surroundCodec: settings.surround_codec,
          surroundBitrateKbps:
            settings.surround_bitrate_kbps === null ? "" : String(settings.surround_bitrate_kbps),
        });
        setState({ status: "ready" });
      })
      .catch((err: unknown) =>
        setState({ status: "error", message: err instanceof Error ? err.message : "Unknown error." }),
      );
  }, []);

  async function handleSave() {
    const validationError = validateGeneralForm(form);
    if (validationError) {
      setError(validationError);
      return;
    }
    setSaving(true);
    setError(null);
    setSavedAt(null);
    try {
      const updated = await updateSettings({
        concurrency_limit: Number(form.concurrencyLimit),
        ui_auth_enabled: form.uiAuthEnabled,
        stereo_codec: form.stereoCodec.trim(),
        stereo_bitrate_kbps: form.stereoBitrateKbps.trim() === "" ? null : Number(form.stereoBitrateKbps),
        surround_codec: form.surroundCodec.trim(),
        surround_bitrate_kbps:
          form.surroundBitrateKbps.trim() === "" ? null : Number(form.surroundBitrateKbps),
      });
      setServerApiKey(updated.api_key);
      setForm({
        concurrencyLimit: String(updated.concurrency_limit),
        uiAuthEnabled: updated.ui_auth_enabled,
        stereoCodec: updated.stereo_codec,
        stereoBitrateKbps: updated.stereo_bitrate_kbps === null ? "" : String(updated.stereo_bitrate_kbps),
        surroundCodec: updated.surround_codec,
        surroundBitrateKbps:
          updated.surround_bitrate_kbps === null ? "" : String(updated.surround_bitrate_kbps),
      });
      setSavedAt(Date.now());
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Unknown error.");
    } finally {
      setSaving(false);
    }
  }

  function handleSaveBrowserKey() {
    setStoredApiKey(browserApiKey);
    setBrowserKeySavedAt(Date.now());
  }

  function handleUseServerKey() {
    setBrowserApiKey(serverApiKey);
    setStoredApiKey(serverApiKey);
    setBrowserKeySavedAt(Date.now());
  }

  return (
    <section className="settings-section">
      <div className="settings-section__header">
        <div>
          <h2 className="settings-section__title">General</h2>
          <p className="settings-section__summary">
            API key, authentication, concurrency, and codec/bitrate overrides.
          </p>
        </div>
      </div>

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading settings…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <p className="panel__message">Couldn&apos;t load settings: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && (
        <>
          <div className="panel settings-form">
            <h3 className="settings-form__subtitle">API key</h3>
            <div className="form-field">
              <label htmlFor="server-api-key">Server API key</label>
              <input id="server-api-key" type="text" readOnly value={serverApiKey} />
              <p className="form-hint">
                Auto-generated by Collapsarr. Sonarr/Radarr-style: send it as the{" "}
                <code>X-Api-Key</code> header on every <code>/api</code> request.
              </p>
            </div>
            <div className="form-field">
              <label htmlFor="browser-api-key">This browser&apos;s stored key</label>
              <input
                id="browser-api-key"
                type="text"
                value={browserApiKey}
                onChange={(event) => setBrowserApiKey(event.target.value)}
              />
              <p className="form-hint">
                Stored locally in this browser and attached to every request this app makes.
              </p>
            </div>
            <div className="form-actions">
              <button type="button" className="btn btn--secondary btn--sm" onClick={handleSaveBrowserKey}>
                Save browser key
              </button>
              <button type="button" className="btn btn--ghost btn--sm" onClick={handleUseServerKey}>
                Use server key
              </button>
              {browserKeySavedAt !== null && <span className="form-success">Saved.</span>}
            </div>
          </div>

          <div className="panel settings-form">
            <h3 className="settings-form__subtitle">Authentication &amp; concurrency</h3>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={form.uiAuthEnabled}
                onChange={(event) => setForm({ ...form, uiAuthEnabled: event.target.checked })}
              />
              Require the API key for UI requests
            </label>

            <div className="form-field form-field--narrow">
              <label htmlFor="concurrency-limit">Concurrency limit</label>
              <input
                id="concurrency-limit"
                type="number"
                min={1}
                value={form.concurrencyLimit}
                onChange={(event) => setForm({ ...form, concurrencyLimit: event.target.value })}
              />
              <p className="form-hint">Maximum downmix jobs running at once.</p>
            </div>
          </div>

          <div className="panel settings-form">
            <h3 className="settings-form__subtitle">Advanced: codec &amp; bitrate overrides</h3>
            <div className="form-grid">
              <div className="form-field">
                <label htmlFor="stereo-codec">Stereo codec</label>
                <input
                  id="stereo-codec"
                  type="text"
                  value={form.stereoCodec}
                  onChange={(event) => setForm({ ...form, stereoCodec: event.target.value })}
                />
              </div>
              <div className="form-field">
                <label htmlFor="stereo-bitrate">Stereo bitrate (kbps)</label>
                <input
                  id="stereo-bitrate"
                  type="number"
                  min={1}
                  placeholder="encoder default"
                  value={form.stereoBitrateKbps}
                  onChange={(event) => setForm({ ...form, stereoBitrateKbps: event.target.value })}
                />
              </div>
              <div className="form-field">
                <label htmlFor="surround-codec">Surround codec</label>
                <input
                  id="surround-codec"
                  type="text"
                  value={form.surroundCodec}
                  onChange={(event) => setForm({ ...form, surroundCodec: event.target.value })}
                />
              </div>
              <div className="form-field">
                <label htmlFor="surround-bitrate">Surround bitrate (kbps)</label>
                <input
                  id="surround-bitrate"
                  type="number"
                  min={1}
                  placeholder="encoder default"
                  value={form.surroundBitrateKbps}
                  onChange={(event) => setForm({ ...form, surroundBitrateKbps: event.target.value })}
                />
              </div>
            </div>

            <div className="form-actions">
              <button type="button" className="btn btn--primary" onClick={handleSave} disabled={saving}>
                {saving ? "Saving…" : "Save general settings"}
              </button>
              {savedAt !== null && !error && <span className="form-success">Saved.</span>}
            </div>
            {error && <p className="form-error">{error}</p>}
          </div>
        </>
      )}
    </section>
  );
}
