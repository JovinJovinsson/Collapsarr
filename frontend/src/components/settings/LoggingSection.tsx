import { useEffect, useState } from "react";

import { fetchSettings, updateSettings } from "../../api/settings";
import type { LogLevel } from "../../types/settings";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready" };

interface LoggingFormValues {
  /**
   * `null` represents "no override" (COL-130 AC2: defer to
   * `COLLAPSARR_LOG_LEVEL` at boot) as a first-class form value -- it must
   * survive an unrelated save undisturbed, so this is never defaulted to a
   * concrete level the way other settings fields are.
   */
  logLevel: LogLevel | null;
}

/** `<select>` value for the "no override" option (COL-130 AC2/AC5) -- HTML
 * select values are always strings, so `null` needs a string sentinel that's
 * mapped back to `null` on change and never collides with a real `LogLevel`. */
const LOG_LEVEL_DEFAULT_OPTION = "__default__";

/**
 * Settings → Logging (COL-147): the seventh of Settings' Bazarr-style
 * sub-nav pages. The `log_level` runtime-override control (select, its
 * options, and its `PUT /api/settings` save call) moved verbatim off
 * `GeneralSection` (where COL-130 originally added it) onto this dedicated
 * page -- every other General field is unaffected by the move.
 */
export function LoggingSection() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [form, setForm] = useState<LoggingFormValues>({ logLevel: null });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchSettings()
      .then((settings) => {
        if (cancelled) return;
        setForm({ logLevel: settings.log_level });
        setState({ status: "ready" });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setState({ status: "error", message: err instanceof Error ? err.message : "Unknown error." });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSave() {
    setSaving(true);
    setError(null);
    setSavedAt(null);
    try {
      const updated = await updateSettings({ log_level: form.logLevel });
      setForm({ logLevel: updated.log_level });
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
          <h2 className="settings-section__title">Logging</h2>
          <p className="settings-section__summary">The runtime log-level override.</p>
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
        <div className="panel settings-form">
          <h3 className="settings-form__subtitle">Log level</h3>
          <div className="form-field form-field--narrow">
            <label htmlFor="log-level">Level</label>
            <select
              id="log-level"
              value={form.logLevel ?? LOG_LEVEL_DEFAULT_OPTION}
              onChange={(event) => {
                const value = event.target.value;
                setForm({
                  ...form,
                  logLevel: value === LOG_LEVEL_DEFAULT_OPTION ? null : (value as LogLevel),
                });
              }}
            >
              <option value={LOG_LEVEL_DEFAULT_OPTION}>Default (env)</option>
              <option value="DEBUG">Debug</option>
              <option value="INFO">Info</option>
              <option value="WARNING">Warning</option>
              <option value="ERROR">Error</option>
            </select>
            <p className="form-hint">
              Applies immediately, without a restart, and persists across restarts. Controls both
              what&apos;s logged and how many rotated log files are kept (more at{" "}
              <strong>Debug</strong>). <strong>Default (env)</strong> (the initial state) defers to
              the <code>COLLAPSARR_LOG_LEVEL</code> environment setting at boot; pick it again here
              to clear a previously-set override.
            </p>
          </div>

          <div className="form-actions">
            <button type="button" className="btn btn--primary" onClick={handleSave} disabled={saving}>
              {saving ? "Saving…" : "Save log level"}
            </button>
            {savedAt !== null && !error && <span className="form-success">Saved.</span>}
          </div>
          {error && <p className="form-error">{error}</p>}
        </div>
      )}
    </section>
  );
}
