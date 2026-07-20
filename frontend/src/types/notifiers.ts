/**
 * Types mirroring the `/api/notifiers` response (COL-36,
 * `collapsarr/notify/routes.py`) -- kept in sync by hand since there's no
 * shared schema generation yet.
 */

/** The persisted notifier config row, decoded to its JSON response shape. */
export interface NotifierConfig {
  webhook_url: string | null;
  webhook_enabled: boolean;
  discord_webhook_url: string | null;
  discord_enabled: boolean;
  created_at: string;
  updated_at: string;
}

/**
 * Request body for `PUT /api/notifiers`. Every field is optional -- only
 * fields present in the request are changed (`collapsarr.notify.routes.
 * NotifierConfigUpdate`'s partial-update convention). Sending an explicit
 * `null` for `webhook_url`/`discord_webhook_url` clears the stored URL;
 * omitting the field leaves it untouched.
 */
export interface NotifierConfigUpdateInput {
  webhook_url?: string | null;
  webhook_enabled?: boolean;
  discord_webhook_url?: string | null;
  discord_enabled?: boolean;
}
