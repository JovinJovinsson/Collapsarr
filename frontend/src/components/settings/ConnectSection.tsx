import { useEffect, useState } from "react";

import { fetchNotifierConfig, updateNotifierConfig } from "../../api/notifiers";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready" };

interface ConnectFormValues {
  webhookUrl: string;
  webhookEnabled: boolean;
  discordWebhookUrl: string;
  discordEnabled: boolean;
}

const EMPTY_FORM: ConnectFormValues = {
  webhookUrl: "",
  webhookEnabled: false,
  discordWebhookUrl: "",
  discordEnabled: false,
};

/**
 * Connect settings (COL-36): the generic webhook and Discord notifiers, each
 * with its own URL + enabled toggle, backed by COL-35's notifier config
 * storage via `GET`/`PUT /api/notifiers`. Notifications fire on downmix
 * failure and app health issues (COL-37/COL-38); this section only owns
 * configuring the two destinations.
 */
export function ConnectSection() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [form, setForm] = useState<ConnectFormValues>(EMPTY_FORM);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    fetchNotifierConfig()
      .then((config) => {
        setForm({
          webhookUrl: config.webhook_url ?? "",
          webhookEnabled: config.webhook_enabled,
          discordWebhookUrl: config.discord_webhook_url ?? "",
          discordEnabled: config.discord_enabled,
        });
        setState({ status: "ready" });
      })
      .catch((err: unknown) =>
        setState({ status: "error", message: err instanceof Error ? err.message : "Unknown error." }),
      );
  }, []);

  async function handleSave() {
    setSaving(true);
    setError(null);
    setSavedAt(null);
    try {
      const updated = await updateNotifierConfig({
        webhook_url: form.webhookUrl.trim() === "" ? null : form.webhookUrl.trim(),
        webhook_enabled: form.webhookEnabled,
        discord_webhook_url: form.discordWebhookUrl.trim() === "" ? null : form.discordWebhookUrl.trim(),
        discord_enabled: form.discordEnabled,
      });
      setForm({
        webhookUrl: updated.webhook_url ?? "",
        webhookEnabled: updated.webhook_enabled,
        discordWebhookUrl: updated.discord_webhook_url ?? "",
        discordEnabled: updated.discord_enabled,
      });
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
          <h2 className="settings-section__title">Connect</h2>
          <p className="settings-section__summary">
            Notify a generic webhook and/or Discord on downmix failure and app health issues.
          </p>
        </div>
      </div>

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading Connect settings…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <p className="panel__message">Couldn&apos;t load Connect settings: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && (
        <div className="panel settings-form">
          <h3 className="settings-form__subtitle">Webhook</h3>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={form.webhookEnabled}
              onChange={(event) => setForm({ ...form, webhookEnabled: event.target.checked })}
            />
            Enable generic webhook notifications
          </label>
          <div className="form-field">
            <label htmlFor="webhook-url">Webhook URL</label>
            <input
              id="webhook-url"
              type="text"
              placeholder="https://example.com/hook"
              value={form.webhookUrl}
              onChange={(event) => setForm({ ...form, webhookUrl: event.target.value })}
            />
            <p className="form-hint">
              Receives a JSON POST on downmix failure and app health issues. Leave blank to clear.
            </p>
          </div>

          <h3 className="settings-form__subtitle">Discord</h3>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={form.discordEnabled}
              onChange={(event) => setForm({ ...form, discordEnabled: event.target.checked })}
            />
            Enable Discord notifications
          </label>
          <div className="form-field">
            <label htmlFor="discord-webhook-url">Discord webhook URL</label>
            <input
              id="discord-webhook-url"
              type="text"
              placeholder="https://discord.com/api/webhooks/…"
              value={form.discordWebhookUrl}
              onChange={(event) => setForm({ ...form, discordWebhookUrl: event.target.value })}
            />
            <p className="form-hint">
              Posts a Discord embed on downmix failure and app health issues. Leave blank to clear.
            </p>
          </div>

          <div className="form-actions">
            <button type="button" className="btn btn--primary" onClick={handleSave} disabled={saving}>
              {saving ? "Saving…" : "Save Connect settings"}
            </button>
            {savedAt !== null && !error && <span className="form-success">Saved.</span>}
          </div>
          {error && <p className="form-error">{error}</p>}
        </div>
      )}
    </section>
  );
}
