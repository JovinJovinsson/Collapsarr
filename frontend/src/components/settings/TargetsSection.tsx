import { useEffect, useState } from "react";

import { fetchSettings, updateSettings } from "../../api/settings";
import type { DownmixTarget } from "../../types/wanted";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready" };

const TARGET_OPTIONS: { value: DownmixTarget; label: string }[] = [
  { value: "stereo", label: "Stereo (2.0)" },
  { value: "2.1", label: "2.1" },
  { value: "5.1", label: "5.1" },
];

/** Parses a comma-separated language-code field into a sorted array, or `null` when blank (allow all). */
function parseLanguageList(text: string): string[] | null {
  const codes = Array.from(
    new Set(
      text
        .split(",")
        .map((code) => code.trim().toLowerCase())
        .filter((code) => code.length > 0),
    ),
  );
  return codes.length > 0 ? codes.sort() : null;
}

/**
 * Downmix targets (Stereo/2.1/5.1 toggles) + language allow-list (COL-33's
 * AC2), backed by COL-28's `GET`/`PUT /api/settings`.
 */
export function TargetsSection() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [enabledTargets, setEnabledTargets] = useState<Set<DownmixTarget>>(new Set());
  const [languageAllowListText, setLanguageAllowListText] = useState("");

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    fetchSettings()
      .then((settings) => {
        setEnabledTargets(new Set(settings.enabled_targets));
        setLanguageAllowListText((settings.language_allow_list ?? []).join(", "));
        setState({ status: "ready" });
      })
      .catch((err: unknown) =>
        setState({ status: "error", message: err instanceof Error ? err.message : "Unknown error." }),
      );
  }, []);

  function toggleTarget(target: DownmixTarget) {
    setEnabledTargets((current) => {
      const next = new Set(current);
      if (next.has(target)) {
        next.delete(target);
      } else {
        next.add(target);
      }
      return next;
    });
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    setSavedAt(null);
    try {
      const updated = await updateSettings({
        enabled_targets: Array.from(enabledTargets),
        language_allow_list: parseLanguageList(languageAllowListText),
      });
      setEnabledTargets(new Set(updated.enabled_targets));
      setLanguageAllowListText((updated.language_allow_list ?? []).join(", "));
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
          <h2 className="settings-section__title">Downmix targets</h2>
          <p className="settings-section__summary">
            Which downmix targets to produce, and which languages to consider.
          </p>
        </div>
      </div>

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading targets…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <p className="panel__message">Couldn&apos;t load settings: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && (
        <div className="panel settings-form">
          <fieldset className="checkbox-group">
            <legend>Enabled targets</legend>
            {TARGET_OPTIONS.map((option) => (
              <label key={option.value} className="checkbox-row">
                <input
                  type="checkbox"
                  checked={enabledTargets.has(option.value)}
                  onChange={() => toggleTarget(option.value)}
                />
                {option.label}
              </label>
            ))}
          </fieldset>

          <div className="form-field">
            <label htmlFor="language-allow-list">Language allow-list</label>
            <input
              id="language-allow-list"
              type="text"
              placeholder="e.g. eng, fre — leave blank to allow every language"
              value={languageAllowListText}
              onChange={(event) => setLanguageAllowListText(event.target.value)}
            />
            <p className="form-hint">Comma-separated language codes. Blank means every language is considered.</p>
          </div>

          <div className="form-actions">
            <button type="button" className="btn btn--primary" onClick={handleSave} disabled={saving}>
              {saving ? "Saving…" : "Save targets"}
            </button>
            {savedAt !== null && !error && <span className="form-success">Saved.</span>}
          </div>
          {error && <p className="form-error">{error}</p>}
        </div>
      )}
    </section>
  );
}
