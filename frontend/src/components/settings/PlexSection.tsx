import { useEffect, useState } from "react";

import { fetchPlexConnection, updatePlexConnection } from "../../api/plex";
import type { PlexConnection, PlexConnectivityStatus } from "../../types/plex";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready" };

interface PlexFormValues {
  baseUrl: string;
  token: string;
}

const EMPTY_FORM: PlexFormValues = { baseUrl: "", token: "" };

const STATUS_LABEL: Record<PlexConnectivityStatus, string> = {
  ok: "Connected",
  error: "Error",
  unknown: "Unknown",
};

/** Validates the Plex connection form; returns an error message, or `null` when valid. */
function validatePlexForm(form: PlexFormValues, hasStoredToken: boolean): string | null {
  if (!form.baseUrl.trim()) return "Base URL is required.";
  try {
    new URL(form.baseUrl.trim());
  } catch {
    return "Base URL must be a valid URL, e.g. http://localhost:32400.";
  }
  if (!form.token.trim() && !hasStoredToken) return "Plex token is required.";
  return null;
}

/**
 * Plex Connection settings (COL-209): a *singleton* config row -- one Plex
 * Media Server, not a CRUD instance list like `InstancesSection`'s
 * Sonarr/Radarr connections. Backed by `GET`/`PUT /api/plex/connection`.
 *
 * The token field is deliberately never pre-filled: the backend's `GET`
 * response has no `token` field at all (COL-209's acceptance criteria -- the
 * Plex token must never reach the browser), only a derived `has_token`
 * boolean. So the field starts blank on every load, with a placeholder
 * explaining that leaving it blank on save keeps the existing token, and the
 * PUT body only includes `token` when the operator actually typed one.
 */
export function PlexSection() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [connection, setConnection] = useState<PlexConnection | null>(null);
  const [form, setForm] = useState<PlexFormValues>(EMPTY_FORM);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  function reload() {
    setState({ status: "loading" });
    fetchPlexConnection()
      .then((loaded) => {
        setConnection(loaded);
        setForm({ baseUrl: loaded.base_url, token: "" });
        setState({ status: "ready" });
      })
      .catch((err: unknown) =>
        setState({ status: "error", message: err instanceof Error ? err.message : "Unknown error." }),
      );
  }

  useEffect(() => {
    reload();
  }, []);

  async function handleSave() {
    const validationError = validatePlexForm(form, connection?.has_token ?? false);
    if (validationError) {
      setError(validationError);
      return;
    }
    setSaving(true);
    setError(null);
    setSavedAt(null);
    try {
      const updated = await updatePlexConnection({
        base_url: form.baseUrl.trim(),
        ...(form.token.trim() ? { token: form.token.trim() } : {}),
      });
      setConnection(updated);
      setForm({ baseUrl: updated.base_url, token: "" });
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
          <h2 className="settings-section__title">Plex</h2>
          <p className="settings-section__summary">
            Connect to a Plex Media Server for Plex-verified default audio track detection. The token is
            stored server-side only and is never sent to the browser.
          </p>
        </div>
      </div>

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading Plex connection…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <p className="panel__message">Couldn&apos;t load Plex connection: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && connection && (
        <div className="panel settings-form">
          <div className="form-grid">
            <div className="form-field">
              <label htmlFor="plex-base-url">Base URL</label>
              <input
                id="plex-base-url"
                type="text"
                placeholder="http://localhost:32400"
                value={form.baseUrl}
                onChange={(event) => setForm({ ...form, baseUrl: event.target.value })}
              />
            </div>
            <div className="form-field">
              <label htmlFor="plex-token">Plex token (X-Plex-Token)</label>
              <input
                id="plex-token"
                type="password"
                placeholder={connection.has_token ? "Configured -- leave blank to keep it" : "Enter your Plex token"}
                value={form.token}
                onChange={(event) => setForm({ ...form, token: event.target.value })}
              />
              <p className="form-hint">
                {connection.has_token
                  ? "A token is already configured. Leave blank to keep it, or enter a new one to replace it."
                  : "Never displayed once saved -- stored and used server-side only."}
              </p>
            </div>
          </div>

          <div className="form-field">
            <span>Status</span>
            <p>
              <span className={`instances-table__status instances-table__status--${connection.status}`}>
                {STATUS_LABEL[connection.status]}
              </span>
              {connection.version && <span> &middot; version {connection.version}</span>}
            </p>
            {connection.status === "error" && connection.status_error && (
              <p className="form-error">{connection.status_error}</p>
            )}
          </div>

          <div className="form-actions">
            <button type="button" className="btn btn--primary" onClick={handleSave} disabled={saving}>
              {saving ? "Saving…" : "Save Plex connection"}
            </button>
            {savedAt !== null && !error && <span className="form-success">Saved.</span>}
          </div>
          {error && <p className="form-error">{error}</p>}
        </div>
      )}
    </section>
  );
}
